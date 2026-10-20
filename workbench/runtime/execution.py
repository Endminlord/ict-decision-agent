"""Bounded execution for explicitly trusted code. This is NOT a sandbox.

Default is deny. Set EXECUTION_MODE=trusted only for reviewed code in a disposable
Linux environment. Limits and a stripped environment are defense in depth, not
filesystem/network isolation. Never enable for arbitrary remote input.
"""
import os
import signal
import subprocess
import tempfile
import time

from runtime import sandbox, safety_tiers
from runtime.audit import record as audit_record


def execution_allowed():
    return os.environ.get('EXECUTION_MODE', 'disabled') == 'trusted'


def run_trusted(command, cwd, timeout=30):
    if not execution_allowed():
        # A refused execution is worth recording too: it says who tried.
        audit_record('command_refused', command=list(command), cwd=str(cwd),
                     reason='execution_disabled')
        raise PermissionError('execution_disabled: set EXECUTION_MODE=trusted only in a trusted/disposable environment; subprocess is not a sandbox')
    started = time.monotonic()
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT') if k in os.environ}
    # Keep the interpreter's installed packages visible when HOME is redirected.
    # Do not inherit an arbitrary caller-supplied PYTHONPATH or API credentials.
    import site
    env['PYTHONPATH'] = os.pathsep.join(site.getsitepackages() + [site.getusersitepackages()])
    env.update(HOME=str(cwd), TMPDIR=str(cwd), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
               PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
    # Wrap with whatever isolation this host can actually enforce, and report
    # the level that was really applied rather than assuming one.
    argv, applied = sandbox.wrap_command(command, workdir=cwd)
    preexec = sandbox.preexec_for()
    # 三档安全执行点（11-B 第 11 项）。分类放在 wrap 之后，因为只有 applied 知道
    # 隔离到底有没有真的生效：便宜的后端（namespace）挡网络但不挡文件系统。
    # report 模式（默认）只记录，enforce 模式在 Popen 之前抛。
    safety_tiers.gate(command, kind="command", caller="run_trusted",
                      isolation_state=safety_tiers.isolation(applied=applied))
    # Report what was actually passed to Popen, not what the host claims to
    # support: the two can differ and only one of them is true.
    applied['limits_applied'] = preexec is not None
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT,
                                start_new_session=os.name == 'posix',
                                preexec_fn=preexec)
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if os.name == 'posix':
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif proc.poll() is None:
                proc.kill()
            proc.wait()
        output.seek(0)
        raw = output.read(65537)
    text = raw[:65536].decode('utf-8', errors='replace')
    if len(raw) > 65536:
        text += '\n[output truncated]'
    exit_code = 124 if timed_out else proc.returncode
    audit_record('command_executed', command=list(command), cwd=str(cwd),
                 exit_code=exit_code, timed_out=timed_out,
                 duration_ms=round((time.monotonic() - started) * 1000, 2),
                 **applied)
    return {'exit_code': exit_code, 'output': text, 'timed_out': timed_out,
            **applied}
