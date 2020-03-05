import subprocess  # noqa: S404


def subprocess_output_iterator(command, env, **kwargs):
    process = subprocess.Popen(  # noqa: S603
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
        env=env,
        **kwargs
    )
    for stdout_line in iter(process.stdout.readline, ""):
        yield stdout_line
    process.stdout.close()
    return_code = process.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)
