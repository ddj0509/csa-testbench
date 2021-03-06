from git import Repo
import sys
import json
import math
from collections import defaultdict


# Implementation of algorithms from:
# https://static.googleusercontent.com/media/research.google.com/hu//pubs/archive/41145.pdf


def main(path):
    words = ["fix", "resolve", "close"]
    repo = Repo(path)
    # Rahman algorithm, rank files based on closed bugs from most bug prone
    # to least bug prone. According to studies, this is almost as good as
    # FixCache.
    rahman = defaultdict(int)
    time_weighted_risk_inter = defaultdict(list)
    # LRU cache. How to fill:
    # Churn locality: recently added/changed files.
    # Temporal locality: a file with a fault might contain more.
    # Spacial locality: files changed together with faulty one might contain
    # more.
    # FIXME: implement.
    fix_cache = {}
    min_time = repo.head.commit.committed_date
    max_time = 0
    for commit in repo.iter_commits():
        if all([w not in commit.message.lower() for w in words]):
            continue
        commit_time = commit.committed_date
        if commit_time < min_time:
            min_time = commit_time
        if commit_time > max_time:
            max_time = commit_time
        for file in commit.stats.files:
            rahman[file] += 1
            time_weighted_risk_inter[file].append(commit_time)
    time_weighted_risk = defaultdict(float)
    time_range = max_time - min_time
    shift = 12  # How strong the decay is.
    for file, times in time_weighted_risk_inter.items():
        # Normalize.
        score = 0
        for time in times:
            time_norm = (time - min_time) / time_range
            score += 1 / (1 + math.exp(-12 * time_norm + shift))
        time_weighted_risk[file] = score
    with open('rahman.txt', 'w') as outfile:
        json.dump(rahman, outfile, indent=2)
    with open('time_weighted_risk.txt', 'w') as outfile:
        json.dump(time_weighted_risk, outfile, indent=2)


if __name__ == "__main__":
    main(sys.argv[1])

