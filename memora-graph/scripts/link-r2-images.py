#!/usr/bin/env python3
"""RETIRED: linked R2 images to memories by writing D1 directly.

It updated `memories.metadata` through the D1 REST API with a Cloudflare
token. memora-all on deploy-host is the only D1 writer
(docs/local-primary-implementation.md §0 P6, §6 F3), so this script now exits
1 for every invocation, `--dry-run` included, before it imports boto3 or
requests or reads any credential. The original is in git history
(before this commit); image linking belongs in memora's own write path.
"""

import sys

if __name__ == "__main__":
    print(
        "link-r2-images.py: retired. It wrote D1 directly; memora-all on deploy-host is the "
        "only D1 writer. See docs/local-primary-implementation.md §0 P6 and §6 F3.",
        file=sys.stderr,
    )
    sys.exit(1)
