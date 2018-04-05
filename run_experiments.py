from __future__ import print_function
import argparse as ap
from collections import Counter
import json
import multiprocessing
import os
import re
import shlex
import shutil
import subprocess as sp
import sys
import tempfile
import zipfile

from summarize_sa_stats import summ_stats
from summarize_gcov import summarize_gcov
from generate_stat_html import HTMLPrinter

TESTBENCH_ROOT = os.getcwd()


def load_config(filename):
    """Load all information from the specified config file."""

    config_path = os.path.join(TESTBENCH_ROOT, filename)
    with open(config_path, 'r') as config_file:
        config_dict = json.loads(config_file.read())
    if not config_dict:
        sys.stderr.write("[Error] Empty config file.\n")
        sys.exit(1)
    return config_dict


def run_command(cmd, print_error=True, cwd=None, env=None, shell=False):
    """Wrapper function to handle running system commands."""

    args = shlex.split(cmd) if not shell else cmd
    proc = sp.Popen(args, stdin=sp.PIPE, stdout=sp.PIPE,
                    stderr=sp.PIPE, cwd=cwd, env=env, shell=shell)
    stdout, stderr = proc.communicate()
    # CC usually does not return with 0, but printing empty
    # error messages in that case is needless.
    if proc.returncode is not 0 and print_error:
        output = stderr if len(stderr) != 0 else stdout
        sys.stderr.write("[ERROR] %s\n" % str(output))
    return proc.returncode, stdout, stderr


def count_lines(project, project_dir):
    failed, stdout, _ = run_command('cloc %s --json --not-match-d="cc_results"' % project_dir)
    if not failed:
        try:
            cloc_json_out = json.loads(stdout)
            project["LOC"] = cloc_json_out["SUM"]["code"]
        except:
            pass
    print("[%s] LOC: %s." % (project['name'],
                             project['LOC'] if 'LOC' in project else '?'))


def clone_project(project, project_dir):
    """Clone a single project.

    Its version is specified by a version tag or a commit hash
    found in the config file.

    If a project already exists, we simply overwrite it.
    """
    if 'prepared' in project:
        count_lines(project, project_dir)
        return True

    # If the project folder already exists, remove it.
    if os.path.isdir(project_dir):
        shutil.rmtree(project_dir)

    # If there is no tag specified, we clone the master branch.
    # This presumes that a master branch exists.
    if 'tag' not in project:
        project['tag'] = 'master'

    # Check whether the project config contains a version tag or a commit hash.
    try:
        int(project['tag'], base=16)
        commit_hash = True
    except:
        commit_hash = False

    # If the 'tag' value is a version tag, we can use shallow cloning.
    # With a commit hash, we need to clone everything and then checkout
    # the specified commit.
    cmd = {
        'clone': 'git clone %s %s' % (project['url'], project_dir)}

    if commit_hash:
        cmd['checkout'] = 'git -C %s checkout %s' % (
            project_dir, project['tag'])
    else:
        cmd['clone'] += ' --depth 1 --branch %s --single-branch' % project['tag']

    print("[%s] Checking out project... " % project['name'])
    sys.stdout.flush()
    clone_failed, _, clone_err = run_command(cmd['clone'], print_error=False)
    if 'master' in str(clone_err):
        clone_failed, _, _ = run_command(
            'git clone %s %s' % (project['url'], project_dir))
    if clone_failed:
        return False
    if 'checkout' in cmd:
        checkout_failed, _, _ = run_command(cmd['checkout'])
        if checkout_failed:
            return False

    count_lines(project, project_dir)

    return True


def identify_build_system(project_dir, configure):
    """Identifies the build system of a project.

    Used heuristics:
        - If there's a 'CMakeLists.txt' file at the project root: 'cmake'.
        - If there's an 'autogen.sh' script at the project root: run it.
        - If there's a 'configure' script at the project root: run it,
          then return 'makefile'.

    FIXME: If no build system found, should we apply the same
           heuristics for src subfolder if exists?

    The actual build-log generation happens in main().
    """

    project_files = os.listdir(project_dir)
    if not project_files:
        sys.stderr.write("[ERROR] No files found in '%s'.\n" % project_dir)
        return None

    if 'CMakeLists.txt' in project_files:
        return 'cmake'

    if 'Makefile' in project_files:
        return 'makefile'

    # When there is a custom configure command,
    # fall back to make files.
    if not configure:
        return 'makefile'

    if 'autogen.sh' in project_files:
        # Autogen needs to be executed in the project's root directory.
        autogen_failed, _, _ = run_command("sh autogen.sh", cwd=project_dir)
        if autogen_failed:
            return None

    # Need to re-list files, as autogen might have generated a config script.
    project_files = os.listdir(project_dir)

    if 'configure' in project_files:
        configure_failed, _, _ = run_command("./configure", cwd=project_dir)
        if configure_failed:
            return None
        return 'makefile'

    sys.stderr.write("[ERROR] Build system cannot be identified.\n")
    return None


def check_logged(projects_root):
    """Post-script cleanup.

    FIXME: instead of listing all directories only list the ones
           that were in the config file. What should we clean up at all?
    """

    projects = os.listdir(projects_root)
    num = 0
    for project in projects:
        if os.path.isfile(os.path.join(projects_root, project)):
            continue
        num += 1
    return num


def log_project(project, project_dir, num_jobs):
    # The following runs the 'configure' script if needed.
    if 'prepared' in project:
        return True
    configure = True
    if 'configure_command' in project:
        _, _, _ = run_command(project['configure_command'],
                              True, project_dir, shell=True)
    if 'make_command' in project:
        build_sys = 'userprovided'
    else:
        build_sys = identify_build_system(project_dir, configure)
    failed = False
    if not build_sys:
        failed = True
    print("[%s] Generating build log... " % project['name'])
    if build_sys == 'cmake':
        cmd = "cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B%s -H%s" \
              % (project_dir, project_dir)
        failed, _, _ = run_command(cmd, True, project_dir)
    elif build_sys == 'makefile':
        json_path = os.path.join(project_dir, "compile_commands.json")
        cmd = "CodeChecker log -b 'make -C%s -j%d' -o %s" \
              % (project_dir, num_jobs, json_path)
        failed, _, _ = run_command(cmd, True, project_dir)
    elif build_sys == 'userprovided':
        json_path = os.path.join(project_dir, "compile_commands.json")
        cmd = "CodeChecker log -b '%s' -o %s" \
              % (project['make_command'], json_path)
        failed, _, _ = run_command(cmd, True, project_dir, shell=True)
    if failed:
        shutil.rmtree(project_dir)
        return False

    return True


def collect_args(arg_name, configuration_sources):
    return " ".join([conf[arg_name] if arg_name in conf else ""
                     for conf in configuration_sources])


def update_path(path, env=None):
    if env is None:
        env = os.environ
    env["PATH"] = path + ":" + env["PATH"]
    return env


def check_project(project, project_dir, config, num_jobs):
    """Analyze project and store the results with CodeChecker."""

    json_path = os.path.join(project_dir, "compile_commands.json")
    if "configurations" not in project:
        if "configurations" in config:
            project["configurations"] = config["configurations"]
        else:
            project["configurations"] = [{"name": ""}]
    for run_config in project["configurations"]:
        result_dir = "cc_results"
        if run_config["name"] != "":
            result_dir += "_" + run_config["name"]
        result_path = os.path.join(project_dir, result_dir)
        coverage_dir = os.path.join(result_path, "coverage")
        run_config["result_path"] = result_path
        run_config["coverage_dir"] = coverage_dir
        args_file, filename = tempfile.mkstemp()
        os.write(args_file, " -Xclang -analyzer-config -Xclang record-coverage=%s "
                 % coverage_dir)
        conf_sources = [config["CodeChecker"], project, run_config]
        os.write(args_file, collect_args("clang_sa_args", conf_sources))
        os.close(args_file)
        tag = project["tag"] if "tag" in project else ""
        name = project["name"]
        if tag != "":
            name += "_" + tag
        if run_config["name"] != "":
            name += "_" + run_config["name"]
        run_config["full_name"] = name
        print("[%s] Analyzing project... " % name)
        sys.stdout.flush()
        env = None
        if "clang_path" in run_config:
            env = update_path(run_config["clang_path"])
        _, version_string, _ = run_command("clang --version", env=env)
        run_config["analyzer_version"] = version_string
        cmd = ("CodeChecker analyze '%s' -j%d -o %s -q " +
               "--analyzers clangsa --capture-analysis-output") \
              % (json_path, num_jobs, result_path)
        cmd += " --saargs " + filename
        cmd += collect_args("analyze_args", conf_sources)
        run_command(cmd, print_error=False, env=env)
        print("[%s] Done. Storing results..." % name)

        cmd = "CodeChecker store %s --url '%s' -n %s --tag %s" \
              % (result_path, config["CodeChecker"]["url"], name, tag)
        cmd += collect_args("store_args", conf_sources)
        run_command(cmd, print_error=False, env=env)
        print("[%s] Results stored." % name)


def create_link(url, text):
    return '<a href="%s">%s</a>' % (url, text)


def process_failures(path, top=5):
    if not os.path.exists(path):
        return 0, 0, [], 0, []
    failures, asserts, errors = 0, Counter(), Counter()
    assert_pattern = re.compile('Assertion.+failed\.')
    error_pattern = re.compile('error: (.+)')
    for name in os.listdir(path):
        if not name.endswith(".zip"):
            continue
        failures += 1
        full_path = os.path.join(path, name)
        with zipfile.ZipFile(full_path) as zip, \
                zip.open("stderr") as stderr:
            for line in stderr:
                match = assert_pattern.search(line)
                if match:
                    asserts[match.group(0)] += 1
                match = error_pattern.search(line)
                if match:
                    errors[match.group(1)] += 1

    return failures, sum(asserts.values()), asserts.most_common(top), \
           sum(errors.values()), errors.most_common(top)


def post_process_project(project, project_dir, config, printer):
    _, stdout, _ = run_command("CodeChecker cmd runs --url %s -o json" % config['CodeChecker']['url'])
    runs = json.loads(stdout)
    project_stats = {}
    for run_config in project["configurations"]:
        cov_result_html = None
        if os.path.isdir(run_config["coverage_dir"]):
            cov_result_path = os.path.join(run_config["result_path"], "coverage_merged")
            try:
                run_command("MergeCoverage.py -i %s -o %s" %
                            (run_config["coverage_dir"], cov_result_path))
            except OSError as oerr:
                print("[Warning] MergeCoverage.py is not found in path.")
            cov_result_html = os.path.join(run_config["result_path"], "coverage.html")
            try:
                run_command("gcovr -k -g %s --html --html-details -r %s -o %s" %
                            (cov_result_path, project_dir, cov_result_html))
            except OSError as oerr:
                print("[Warning] gcovr is not found in path.")
            cov_summary = summarize_gcov(cov_result_path)
            cov_summary_path = os.path.join(run_config["result_path"], "coverage.txt")
            with open(cov_summary_path, "w") as cov_file:
                cov_file.write(json.dumps(cov_summary, indent=2))

        stats_dir = os.path.join(run_config["result_path"], "success")
        failed_dir = os.path.join(run_config["result_path"], "failed")

        # Statistics from the Analyzer engine (if enabled).
        stats = summ_stats(stats_dir, False)

        # Additional statistics.
        stats["Analyzer version"] = run_config["analyzer_version"]
        if cov_result_html:
            stats["Detailed coverage link"] = create_link(cov_result_html, "coverage");
            stats["Coverage"] = cov_summary["overall"]["coverage"]
        for run in runs:
            if run_config['full_name'] in run:
                run = run[run_config['full_name']]
                break
        stats["Result count"] = run["resultCount"]
        stats["Duration"] = run["duration"]
        stats["CodeChecker link"] = create_link("%s/#run=%s&tab=%s" %
                                                (config['CodeChecker']['url'], run_config['full_name'],
                                                 run_config['full_name']),
                                                "CodeChecker")
        stats["Successfully analyzed"] = \
            len([name for name in os.listdir(run_config["result_path"])
                 if name.endswith(".plist")])
        failures, asserts, assert_toplist, errors, error_toplist = \
            process_failures(failed_dir)
        stats["Failed to analyze"] = failures
        stats["Compiler errors"] = errors
        stats["Number of assertions"] = asserts
        if assert_toplist:
            assert_toplist = map(lambda x: "%s [%d]" % x, assert_toplist)
            stats["Top asserts"] = "<br>\n".join(assert_toplist)
        if error_toplist:
            error_toplist = map(lambda x: "%s [%d]" % x, error_toplist)
            stats["Top errors"] = "<br>\n".join(error_toplist)
        if "LOC" in project:
            stats["Lines of code"] = project["LOC"]

        project_stats[run_config["name"]] = stats

    printer.extend_with_project(project["name"], project_stats)
    print("[%s] Post processed." % project['name'])


def main():
    parser = ap.ArgumentParser(description="Run differential analysis " +
                                           "experiment on a set of projects.",
                               formatter_class=ap.RawTextHelpFormatter)
    parser.add_argument("--config", metavar="FILE",
                        default='test_config.json',
                        help="JSON file holding a list of projects")
    parser.add_argument("-j", "--jobs", metavar="JOBS", type=int,
                        default=multiprocessing.cpu_count(),
                        help="number of jobs")
    args = parser.parse_args()

    try:
        run_command("CodeChecker version")
    except OSError as oerr:
        sys.stderr.write(
            "[ERROR] CodeChecker is not available as a command.\n")
        sys.exit(1)

    if args.jobs < 1:
        sys.stderr.write(
            "[ERROR] The number of jobs must be a positive integer.\n")

    config_path = args.config
    print("Using configuration file '%s'." % config_path)
    config = load_config(config_path)
    print("Number of projects to process: %d.\n" % len(config['projects']))

    projects_root = os.path.join(TESTBENCH_ROOT, 'projects')
    if not os.path.isdir(projects_root):
        os.mkdir(projects_root)

    stats_html = os.path.join(projects_root, "stats.html")
    printer = HTMLPrinter(stats_html, config)

    for project in config['projects']:
        project_dir = os.path.join(projects_root, project['name'])
        if not clone_project(project, project_dir):
            shutil.rmtree(project_dir)
            continue
        if not log_project(project, project_dir, args.jobs):
            continue
        check_project(project, project_dir, config, args.jobs)
        post_process_project(project, project_dir, config, printer)

    printer.finish()

    logged_projects = check_logged(projects_root)
    print("Number of analyzed projects: %d / %d"
          % (logged_projects, len(config['projects'])))
    print("Results can be viewed at '%s'." % config['CodeChecker']['url'])


if __name__ == '__main__':
    main()
