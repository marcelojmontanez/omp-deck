import json


def plugin_record(omp_bin, plugin_name, *, runner):
    result = runner(
        [omp_bin, "plugin", "list", "--json"],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("cannot inspect installed OMP plugins")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("OMP plugin inventory returned invalid JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("npm"), list):
        raise RuntimeError("OMP plugin inventory returned an invalid result")
    records = payload["npm"]
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in records):
        raise RuntimeError("OMP plugin inventory returned an invalid plugin record")
    return next((item for item in records if item["name"] == plugin_name), None)
