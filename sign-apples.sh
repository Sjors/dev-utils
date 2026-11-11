#!/bin/bash
# Keep passphrases out of shell tracing, including when invoked with bash -x.
set +x
set -euo pipefail

usage() {
    cat <<'HELP'
Usage: sign-apples.sh [OPTIONS] --input-dir DIR --output-dir DIR [--] TARGET...
  or: sign-apples.sh [OPTIONS] (--input-dir DIR | --remote HOST) [--output-dir DIR] --project sv2-tp --release VERSION

Create detached macOS signatures, apply them, and notarize each target.
TARGETs are paths relative to --input-dir: Mach-O binaries or .app bundles.
The --output-dir mirrors those paths and contains detached signatures
and, for app bundles, detached notarization tickets. Inputs are modified.
Project mode selects both Apple host triples and all supported binary names.
sv2-tp signs sv2-tp and test_sv2 for arm64-apple-darwin and x86_64-apple-darwin.

Options:
  --env-file FILE     Configuration file (default: .env in working directory,
                      if present). Command-line options override configuration
  --certificate FILE  PKCS#12 signing certificate and private key
  --api-key FILE      App Store Connect API private key (AuthKey_<ID>.p8)
  --key-id ID         App Store Connect API key ID
  --issuer UUID       App Store Connect issuer ID
  --input-dir DIR     Root containing already extracted, unsigned targets
  --remote HOST       Fetch Guix codesigning archives over SCP (project mode)
  --remote-repo DIR   Remote checkout (default: project name, relative to home)
  --output-dir DIR    Signatures directory; may already contain other releases
  --project NAME      Select supported project targets (currently: sv2-tp)
  --release VERSION   Release directory version; required with --project
  -h, --help          Show this help
  --version          Show version

Environment:
  SIGNAPPLE_CERTIFICATE        Alternative to --certificate
  SIGNAPPLE_API_KEY            Alternative to --api-key
  SIGNAPPLE_KEY_ID             Alternative to --key-id
  SIGNAPPLE_ISSUER             Alternative to --issuer
  SIGNAPPLE_PASSPHRASE         Signing key passphrase; prompt once if unset
  SIGNAPPLE_NOTARY_PASSPHRASE  API key passphrase; defaults to signing passphrase
                             Set explicitly to empty for an unencrypted API key
  SV2_TP_DETACHED_SIGS_DIR     Repository root; defaults sv2-tp output to its osx/
                             directory when --output-dir is omitted

These variables can also be set in .env (see .env.example). Precedence:
command-line options, then environment, then file. File values are literal,
optionally quoted, without shell expansion. Paths in the file may
start with ~/; relative paths in the file resolve against its directory.

Remote mode replaces --input-dir and requires --project and --release.
It fetches <repo>/guix-build-<release>/output/<host>/
<project>-<release>-<host>-codesigning.tar.gz for both Apple hosts.
Archives and extracted inputs are temporary and removed on exit.
Use an SSH alias or [user@]hostname; configure ports in ~/.ssh/config.

Requires Bash 3.2+, Git, signapple, Python 3, and xcrun notarytool (macOS).
Every notarization must have a top-level JSON status of Accepted.
Output is added only after all targets succeed. Existing files are never
overwritten; a conflicting path fails publication. Inputs may already be modified.
Use fresh extracted inputs when retrying local mode. When output is in a Git
worktree, only this run's signatures are committed; other changes are preserved.
Plain output directories are supported. No push runs. If committing fails,
signatures remain in the output directory for a manual commit.
HELP
}

die() {
    echo "Error: $*" >&2
    exit 1
}

certificate=""
api_key=""
key_id=""
issuer=""
input_dir=""
remote=""
remote_repo=""
output_dir=""
env_file=".env"
explicit_env_file=false
project=""
release=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --version) echo 'sign-apples.sh 1.0'; exit 0 ;;
        --certificate|--api-key|--key-id|--issuer|--input-dir|--output-dir|--env-file|--project|--release|--remote|--remote-repo)
            [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || die "Missing value for $1"
            case "$1" in
                --certificate) certificate="$2" ;;
                --api-key) api_key="$2" ;;
                --key-id) key_id="$2" ;;
                --issuer) issuer="$2" ;;
                --input-dir) input_dir="$2" ;;
                --remote) remote="$2" ;;
                --remote-repo) remote_repo="$2" ;;
                --output-dir) output_dir="$2" ;;
                --env-file) env_file="$2"; explicit_env_file=true ;;
                --project) project="$2" ;;
                --release) release="$2" ;;
            esac
            shift 2 ;;
        --) shift; break ;;
        -*) die "Unknown option: $1" ;;
        *) break ;;
    esac
done

if [[ "$explicit_env_file" == true || -e "$env_file" || -L "$env_file" ]]; then
    [[ -f "$env_file" && -r "$env_file" ]] || die "Unreadable environment file: $env_file"
    command -v python3 >/dev/null || die "Required command not found: python3"
    # Parse data rather than sourcing shell code. Assignment records retain
    # empty values and embedded '='; malformed files fail before any signing.
    env_data=$(python3 - "$env_file" <<'PY'
import pathlib, re, shlex, sys

allowed = {"SIGNAPPLE_CERTIFICATE", "SIGNAPPLE_API_KEY", "SIGNAPPLE_KEY_ID",
           "SIGNAPPLE_ISSUER", "SIGNAPPLE_PASSPHRASE", "SIGNAPPLE_NOTARY_PASSPHRASE",
           "SV2_TP_DETACHED_SIGS_DIR"}
paths = {"SIGNAPPLE_CERTIFICATE", "SIGNAPPLE_API_KEY", "SV2_TP_DETACHED_SIGS_DIR"}
source = pathlib.Path(sys.argv[1]).resolve()
seen = set()
try:
    for number, line in enumerate(source.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)", line)
        if not match or match[1] not in allowed:
            raise ValueError(f"unsupported setting on line {number}")
        if match[1] in seen:
            raise ValueError(f"duplicate setting on line {number}")
        seen.add(match[1])
        values = shlex.split(match[2], comments=True)
        if len(values) > 1:
            raise ValueError(f"quote values containing spaces on line {number}")
        value = values[0] if values else ""
        if "\0" in value:
            raise ValueError(f"NUL byte on line {number}")
        if match[1] in paths and value:
            path = pathlib.Path(value).expanduser()
            value = str(path if path.is_absolute() else source.parent / path)
        print(f"{match[1]}={value}")
except (OSError, UnicodeError, ValueError) as error:
    sys.exit(f"Error: Cannot load environment file: {error}")
PY
    ) || die "Could not load environment file: $env_file"
    while IFS= read -r assignment; do
        [[ -n "$assignment" ]] || continue
        name="${assignment%%=*}"
        # Preserve even an explicitly empty environment value.
        if ! declare -p "$name" >/dev/null 2>&1; then
            printf -v "$name" '%s' "${assignment#*=}"
        fi
    done <<< "$env_data"
    unset env_data assignment value
fi
certificate="${certificate:-${SIGNAPPLE_CERTIFICATE:-}}"
api_key="${api_key:-${SIGNAPPLE_API_KEY:-}}"
key_id="${key_id:-${SIGNAPPLE_KEY_ID:-}}"
issuer="${issuer:-${SIGNAPPLE_ISSUER:-}}"

if [[ -n "$project" ]]; then
    [[ $# -eq 0 ]] || die "Do not combine --project with explicit targets"
    [[ "$release" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || die "--project requires a valid --release version"
    case "$project" in
        sv2-tp)
            if [[ -z "$output_dir" && -n "${SV2_TP_DETACHED_SIGS_DIR:-}" ]]; then
                output_dir="${SV2_TP_DETACHED_SIGS_DIR%/}/osx"
            fi
            set -- "arm64-apple-darwin/sv2-tp-$release/bin/sv2-tp" \
                "arm64-apple-darwin/sv2-tp-$release/bin/test_sv2" \
                "x86_64-apple-darwin/sv2-tp-$release/bin/sv2-tp" \
                "x86_64-apple-darwin/sv2-tp-$release/bin/test_sv2" ;;
        *) die "Unsupported project: $project (supported: sv2-tp)" ;;
    esac
elif [[ -n "$release" ]]; then
    die "--release requires --project"
fi
if [[ -n "$remote" ]]; then
    [[ -z "$input_dir" ]] || die "Do not combine --remote with --input-dir"
    [[ -n "$project" ]] || die "--remote requires --project and --release"
    [[ "$remote" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.@-]*$ ]] || die "Use an SSH alias or [user@]hostname"
    remote_repo="${remote_repo:-$project}"
    # Safe for both SFTP scp and legacy scp's remote shell.
    [[ "$remote_repo" =~ ^[a-zA-Z0-9_./-]+$ && "$remote_repo" != -* ]] || die "Remote path must use only letters, digits, underscores, dots, slashes or hyphens"
elif [[ -n "$remote_repo" ]]; then
    die "--remote-repo requires --remote"
fi
[[ -n "$certificate" && -n "$api_key" && -n "$key_id" && -n "$issuer" &&
   ( -n "$input_dir" || -n "$remote" ) && -n "$output_dir" && $# -gt 0 ]] || {
    usage >&2
    die "Credentials, an input directory or remote, output directory, and at least one target are required"
}
[[ "$key_id" =~ ^[a-zA-Z0-9]+$ ]] || die "API key ID must contain only letters and digits"

# Absolute paths keep leading dashes and spaces safe in external commands.
for name in certificate api_key input_dir output_dir; do
    value="${!name}"
    [[ -z "$value" || "$value" == /* ]] || printf -v "$name" '%s/%s' "$PWD" "$value"
done
[[ -f "$certificate" && -r "$certificate" ]] || die "Unreadable certificate: $certificate"
[[ -f "$api_key" && -r "$api_key" ]] || die "Unreadable API key: $api_key"
[[ "$(basename "$api_key")" == *AuthKey_"$key_id".p8 ]] || die "API key filename must end in AuthKey_${key_id}.p8 (signapple derives its key ID from this name)"
# Strip trailing slashes so even a dangling output symlink is refused.
while [[ "$output_dir" == */ ]]; do output_dir="${output_dir%/}"; done
[[ -n "$output_dir" && ! -L "$output_dir" && ( ! -e "$output_dir" || -d "$output_dir" ) ]] || die "Output must be a directory, not a file or symlink: $output_dir"

trap 'unset SIGNAPPLE_PASSPHRASE SIGNAPPLE_NOTARY_PASSPHRASE; if [[ -n "${work_dir:-}" ]]; then rm -rf -- "$work_dir"; fi; if [[ -n "${fetch_dir:-}" ]]; then rm -rf -- "$fetch_dir"; fi' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -n "$remote" ]]; then
    for command in scp python3 mktemp; do
        command -v "$command" >/dev/null || die "Required command not found: $command"
    done
    fetch_dir=$(mktemp -d "${TMPDIR:-/tmp}/.sign-apples.fetch.XXXXXXXX")
    input_dir="$fetch_dir/inputs"
    mkdir "$input_dir"
    # One remote glob source keeps both downloads on one SSH connection. Passing
    # two remote source arguments can make scp open separate connections.
    echo "Fetching Apple codesigning archives from $remote"
    scp "$remote:$remote_repo/guix-build-$release/output/*-apple-darwin/$project-$release-*-apple-darwin-codesigning.tar.gz" \
        "$fetch_dir/" || die "Could not fetch Guix codesigning archives"
    for host in arm64-apple-darwin x86_64-apple-darwin; do
        archive="$project-$release-$host-codesigning.tar.gz"
        mkdir "$input_dir/$host"
        # Guix archives include helper scripts beside the release directory.
        # Extract regular files/directories without executing helpers or allowing
        # archive paths or links to write outside the private input root.
        python3 - "$fetch_dir/$archive" "$input_dir/$host" <<'PYREMOTE'
import pathlib, shutil, sys, tarfile

archive, root = sys.argv[1:]
try:
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        for member in members:
            # tar -C <directory> . includes a harmless extraction-root entry.
            if member.isdir() and member.name in (".", "./"):
                continue
            path = pathlib.PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or not (member.isdir() or member.isfile())):
                raise ValueError(f"Unsafe archive entry: {member.name}")
        for member in members:
            destination = pathlib.Path(root) / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, destination.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                destination.chmod(member.mode & 0o777)
except (OSError, ValueError, tarfile.TarError) as error:
    sys.exit(f"Error: Could not extract {archive}: {error}")
PYREMOTE
    done
fi
[[ -d "$input_dir" ]] || die "Input directory does not exist: $input_dir"
input_dir=$(cd "$input_dir" && pwd -P)

targets=("$@")
checked=()
for target in "${targets[@]}"; do
    case "/$target/" in
        *'//'*|*'/../'*|*'/./'*|*$'\n'*) die "Target must be a normalized relative path without newlines: $target" ;;
    esac
    [[ -n "$target" && ! -L "$input_dir/$target" ]] || die "Invalid or symlinked target: $target"
    if [[ ! -f "$input_dir/$target" ]]; then
        [[ "$target" == *.app && -d "$input_dir/$target" ]] || die "Missing binary or .app bundle: $target"
    fi
    # Reject symlinked parent directories as well as symlinked targets.
    parent="$input_dir"
    IFS=/ read -r -a components <<< "$target"
    for component in "${components[@]}"; do
        parent="$parent/$component"
        [[ ! -L "$parent" ]] || die "Symlink in target path: $target"
    done
    # Bash 3.2 with nounset needs this expansion for an empty array.
    for previous in "${checked[@]+"${checked[@]}"}"; do
        [[ "$target" != "$previous" && "$target" != "$previous/"* &&
           "$previous" != "$target/"* ]] || die "Duplicate or overlapping targets: $previous, $target"
    done
    checked+=("$target")
done

for command in git signapple xcrun python3 mktemp tee sed tail; do
    command -v "$command" >/dev/null || die "Required command not found: $command"
done
signapple --help >/dev/null
xcrun --find notarytool >/dev/null

if [[ ! ${SIGNAPPLE_PASSPHRASE+x} ]]; then
    [[ -t 0 ]] || die "Set SIGNAPPLE_PASSPHRASE when standard input is not a terminal"
    IFS= read -r -s -p 'Enter signing key passphrase: ' SIGNAPPLE_PASSPHRASE || die "Could not read passphrase"
    echo
fi
SIGNAPPLE_NOTARY_PASSPHRASE="${SIGNAPPLE_NOTARY_PASSPHRASE-$SIGNAPPLE_PASSPHRASE}"

output_parent=$(dirname "$output_dir")
mkdir -p "$output_parent"
output_parent=$(cd "$output_parent" && pwd -P)
output_dir="$output_parent/$(basename "$output_dir")"
for target in "${targets[@]}"; do
    [[ "$output_dir" != "$input_dir/$target" && "$output_dir" != "$input_dir/$target/"* ]] || die "Output must be outside the target: $target"
done
work_dir=$(mktemp -d "$output_parent/.sign-apples.XXXXXXXX")
mkdir "$work_dir/signatures"

notarize_and_verify() {
    local target="$1" stage="$2" notarization_id notary_info
    # pipefail propagates a signapple failure even if tee succeeds.
    if ! signapple notarize --passphrase "$SIGNAPPLE_NOTARY_PASSPHRASE" \
        --detach "$stage" "$api_key" "$issuer" "$target" 2>&1 | tee "$work_dir/notarize.log"; then
        die "Notarization failed for $target"
    fi
    notarization_id=$(sed -n 's/^Notarization ID: //p' "$work_dir/notarize.log" | tail -n 1)
    [[ "$notarization_id" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]] || die "Missing or invalid notarization ID for $target"
    echo "Checking notarization verdict for $notarization_id"
    notary_info=$(xcrun notarytool info "$notarization_id" --key "$api_key" \
        --key-id "$key_id" --issuer "$issuer" --output-format json)
    printf '%s\n' "$notary_info"
    if ! printf '%s\n' "$notary_info" | python3 -c '
import json, sys
try:
    info = json.load(sys.stdin)
    accepted = isinstance(info, dict) and info.get("status") == "Accepted"
except (ValueError, TypeError):
    accepted = False
sys.exit(0 if accepted else 1)
'; then
        die "Notarization verdict is not Accepted for $target (ID: $notarization_id)"
    fi
}

for target in "${targets[@]}"; do
    target_path="$input_dir/$target"
    stage="$work_dir/target"
    mkdir "$stage"
    echo "Signing $target"
    signapple sign --passphrase "$SIGNAPPLE_PASSPHRASE" --hardened-runtime \
        "$certificate" "$target_path" --detach "$stage"
    # Applying the isolated directory handles all slices of universal binaries
    # together, as well as app bundle resources. Verify the target explicitly:
    # signapple's apply command can report failure without a nonzero exit code.
    signapple apply --no-verify "$(dirname "$target_path")" "$stage"
    signapple verify "$target_path"
    notarize_and_verify "$target_path" "$stage"
    destination="$work_dir/signatures/$(dirname "$target")"
    mkdir -p "$destination"
    cp -R "$stage/." "$destination/"
    rm -rf -- "$stage"
done

# Merge only after every target has succeeded. Check the entire destination
# before writing, then create files exclusively so existing releases survive.
# Roll back this run's additions if publication fails partway through.
python3 - "$work_dir/signatures" "$output_dir" <<'PY'
import pathlib, shutil, sys

source, output = map(pathlib.Path, sys.argv[1:])
directories = [output]
files = []
created_files = []
created_directories = []

def check_directory(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"Output path is not a directory or is a symlink: {path}")

try:
    for path in sorted(source.rglob("*")):
        destination = output / path.relative_to(source)
        if path.is_symlink():
            raise ValueError(f"Unexpected staged symlink: {path}")
        if path.is_dir():
            directories.append(destination)
        elif path.is_file():
            files.append((path, destination))
        else:
            raise ValueError(f"Unexpected staged file type: {path}")
    for directory in directories:
        check_directory(directory)
    for _, destination in files:
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"Refusing to overwrite existing output: {destination}")
    for directory in directories:
        try:
            directory.mkdir()
            created_directories.append(directory)
        except FileExistsError:
            check_directory(directory)
    for path, destination in files:
        with path.open("rb") as incoming, destination.open("xb") as outgoing:
            created_files.append(destination)
            shutil.copyfileobj(incoming, outgoing)
except (OSError, ValueError, KeyboardInterrupt) as error:
    # Remove files before their parent directories; retain pre-existing paths.
    for paths, remove in ((created_files, pathlib.Path.unlink),
                          (created_directories, pathlib.Path.rmdir)):
        for path in reversed(paths):
            try:
                remove(path)
            except OSError as cleanup_error:
                print(f"Error: Could not roll back {path}: {cleanup_error}", file=sys.stderr)
    sys.exit(f"Error: Could not publish signatures: {error}")
PY
echo "Signatures written to $output_dir"

# Use exact staged output paths, rather than adding the output tree, so unrelated
# working-tree and index changes are preserved. A Git failure leaves the signed
# artifacts available for a manual commit without repeating notarization.
python3 - "$work_dir/signatures" "$output_dir" "$release" <<'PY'
import pathlib, subprocess, sys

source, output = map(pathlib.Path, sys.argv[1:3])
release = sys.argv[3]
git = ["git", "--literal-pathspecs", "-C", str(output)]
inside = subprocess.run([*git, "rev-parse", "--is-inside-work-tree"],
                        capture_output=True, text=True)
if inside.returncode or inside.stdout.strip() != "true":
    print("Output is outside a Git worktree; signatures were not committed")
    sys.exit(0)
files = sorted(str(path.relative_to(source)) for path in source.rglob("*") if path.is_file())
message = f"Add {release} detached macOS signatures" if release else "Add detached macOS signatures"
try:
    subprocess.run([*git, "add", "--", *files], check=True)
    subprocess.run([*git, "commit", "--only", "-m", message, "--", *files], check=True)
except (OSError, subprocess.CalledProcessError) as error:
    sys.exit(f"Error: Could not commit signatures: {error}. "
             f"Signatures remain in {output}; commit them manually.")
PY
