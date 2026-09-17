import re
import subprocess

_OMP_VERSION_PATTERN = re.compile(
    r"\Aomp/([0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)\Z"
)


def installed_omp_version(omp_bin, *, runner=subprocess.run, timeout=2):
    try:
        result = runner(
            [omp_bin, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            return None
        output = result.stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = _OMP_VERSION_PATTERN.fullmatch((output or "").strip())
    return match.group(1) if match else None
