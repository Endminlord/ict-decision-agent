import os
import sys
import tempfile
from runtime.executor import parse_case
from runtime.execution import run_trusted
from runtime.security_scanner import scan_code


def performance_tester(code, llm=None, test_cases=None):
    report = {'metrics': {}, 'stub': True}
    if not isinstance(code, dict) or not isinstance(code.get('code'), str) or not code['code'].strip():
        return {'performance_tester': report}
    try:
        if code.get('language', 'python') != 'python':
            raise ValueError('unsupported_language')
        if scan_code(code['code'])['verdict'] == 'fail':
            raise ValueError('security_scan_failed')
        cases = (test_cases or {}).get('cases', [])
        if not cases:
            raise ValueError('missing_benchmark_arguments')
        name, args, kwargs, _ = parse_case(cases[0])
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'green_code.py'), 'w', encoding='utf-8') as f:
                f.write(code['code'])
            script = ('import green_code,timeit\n'
                      'elapsed=timeit.timeit(lambda: green_code.' + name + '(*' + repr(args) + ', **' + repr(kwargs) + '),number=100)\n'
                      'print("BENCHMARK_SECONDS="+str(elapsed))\n')
            result = run_trusted([sys.executable, '-c', script], tmp, 30)
            if result['exit_code'] != 0:
                raise RuntimeError(result['output'][-500:] or 'benchmark_failed')
            value = float(result['output'].rsplit('BENCHMARK_SECONDS=', 1)[1].strip())
            report.update(metrics={'execution_time_100_calls': value, 'case_id': cases[0].get('id'), 'calls': 100}, stub=False)
    except Exception as exc:
        report['metrics'] = {'error': str(exc)[:1000]}
    return {'performance_tester': report}
