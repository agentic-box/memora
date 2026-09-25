#!/usr/bin/env bash
# Mint one memora /api/v1 token (API1). The USER runs this on the deploy
# host; it prints NOTHING about the token.
#
#   scripts/mint_api_token.sh --stores memora,re --out ~/.config/clmuxd/memora-api.token
#       [--dir ~/.config/memora-lp] [--tokens-file api-tokens.json]
#
# - generates a random token (32 bytes, urlsafe base64) and writes it, and
#   only it, to --out: a NEW file, mode 0600 (an existing file is refused);
# - adds sha256(token) -> [stores] to <dir>/<tokens-file>, the server's
#   tokens file (memora/api_v1.py parse_token_table;
#   contracts/memora-api/v1/README.md): created 0600 if absent, written
#   atomically, keeping its mode; an existing digest is never overwritten;
#   the file must stay a regular 0600 file owned by this user, at most 4 KiB.
# The deploy (scripts/deploy-memora-all.sh) turns /api/v1 on when that
# tokens file exists in its secrets directory. A plain token for the deploy's
# own smoke check can be minted the same way with --out <dir>/api-smoke.token.
# Exit 0 on success, 2 on a refusal (nothing written).
set -euo pipefail

STORES=""; OUT=""; DIR="$HOME/.config/memora-lp"; NAME="api-tokens.json"
while [ $# -gt 0 ]; do
  case "$1" in
    --stores) STORES="${2:-}"; shift ;;
    --out) OUT="${2:-}"; shift ;;
    --dir) DIR="${2:-}"; shift ;;
    --tokens-file) NAME="${2:-}"; shift ;;
    -h|--help) awk 'NR > 1 && /^set -euo/ { exit } NR > 1' "$0"; exit 0 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$STORES" ] && [ -n "$OUT" ] || { echo "usage: $0 --stores a,b --out FILE [--dir DIR] [--tokens-file NAME]" >&2; exit 2; }

exec python3 - "$STORES" "$OUT" "$DIR" "$NAME" <<'PY'
import base64, hashlib, json, os, re, secrets, stat, sys, tempfile

stores_arg, out, directory, name = sys.argv[1:]
NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")          # memora/api_v1.py NAME_RE
MAX = 4096                                           # api_v1.TOKEN_FILE_MAX_BYTES


def refuse(msg):
    print(f"refused: {msg} -- nothing was written", file=sys.stderr)
    sys.exit(2)


stores = [s.strip() for s in stores_arg.split(",") if s.strip()]
if not stores or not all(NAME_RE.match(s) for s in stores):
    refuse(f"--stores must be store names matching {NAME_RE.pattern}")
if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
    refuse("--tokens-file must be a plain file name")
out = os.path.abspath(os.path.expanduser(out))
if os.path.lexists(out):
    refuse(f"{out} already exists")
directory = os.path.abspath(os.path.expanduser(directory))
st = None
try:
    st = os.lstat(directory)
except OSError:
    refuse(f"{directory} does not exist")
if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
    refuse(f"{directory} must be a directory (not a symlink) owned by this user")
path = os.path.join(directory, name)
if os.path.realpath(out) == os.path.realpath(path):
    refuse("--out must not be the tokens file itself")
table = {}
if os.path.lexists(path):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
        refuse(f"{path} must be a regular file owned by this user with mode 0600")
    try:
        table = json.loads(open(path, encoding="utf-8").read() or "{}")
    except ValueError:
        refuse(f"{path} is not valid JSON")
    if not isinstance(table, dict):
        refuse(f"{path} is not a JSON object")

token = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
if digest in table:
    refuse("the new token's digest is already in the tokens file")
table[digest] = sorted(dict.fromkeys(stores))
body = (json.dumps(table, indent=2, sort_keys=True) + "\n").encode("utf-8")
if len(body) > MAX:
    refuse(f"the tokens file would exceed {MAX} bytes")

# The plain token first (a new 0600 file), then the digest (atomic replace).
# Never write both to the same path (checked above): the digest write would
# silently replace the plain token, leaving an authorized digest nobody can
# use (review 8232). A UNIQUE temp file next to the tokens file, and only a
# temp this invocation created is ever removed.
created_out = False
tmp = None
try:
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created_out = True
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    fd, tmp = tempfile.mkstemp(prefix=name + ".", suffix=".mint-tmp", dir=directory)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    tmp = None
except BaseException as exc:
    if tmp is not None:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if created_out:
        try:
            os.unlink(out)  # no orphan token without its digest
        except OSError:
            pass
    if isinstance(exc, OSError):
        refuse(f"cannot write the token and digest files: {exc}")
    raise
PY
