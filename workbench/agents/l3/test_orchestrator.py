"""Generate actual assertions, execute pytest and report structured evidence."""
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from runtime.executor import assertion_source
from runtime.execution import run_trusted
from runtime.security_scanner import scan_code


def _generate_pytest(cases, language='python'):
    if language != 'python':
        raise ValueError('unsupported_language: ' + str(language))
    lines = ['import sys', 'from pathlib import Path',
             'sys.path.insert(0, str(Path(__file__).parent))', 'import green_code', '']
    for i, case in enumerate(cases):
        # Numeric identifiers avoid syntax errors and injection through case IDs.
        lines += ['def test_case_' + str(i) + '():', '    ' + assertion_source(case), '']
    return '\n'.join(lines)


def test_orchestrator(tests, llm=None, green_code=None):
    report = {'suite': [], 'passed': 0, 'failed': 0, 'errors': 0, 'skipped': 0,
              'pass_rate': 0.0, 'coverage': None, 'coverage_kind': 'not_measured',
              'success': False, 'stub': False, 'isolated': False}
    def finish():
        return {'test_orchestrator': report}
    if not isinstance(tests, dict) or not isinstance(tests.get('cases'), list) or not tests['cases']:
        report.update(stub=True, error='missing_test_cases')
        return finish()
    if len(tests['cases']) > 100:
        report.update(errors=1, error='too_many_cases')
        return finish()
    code = green_code if green_code is not None else tests.get('green_code')
    if isinstance(code, dict):
        language = code.get('language', tests.get('language', 'python'))
        code = code.get('code')
    else:
        language = tests.get('language', 'python')
    if not isinstance(code, str) or not code.strip():
        report.update(errors=1, error='missing_green_code')
        return finish()
    try:
        if language != 'python':
            raise ValueError('unsupported_language: ' + str(language))
        scan = scan_code(code)
        if scan['verdict'] == 'fail':
            raise ValueError('security_scan_failed: ' + str(scan['findings']))
        generated = _generate_pytest(tests['cases'], language)
        with tempfile.TemporaryDirectory() as tmp:
            for name, source in (('green_code.py', code), ('test_generated.py', generated)):
                with open(os.path.join(tmp, name), 'w', encoding='utf-8') as f:
                    f.write(source)
            result = run_trusted([sys.executable, '-m', 'pytest', '-q', 'test_generated.py',
                                  '--tb=short', '-p', 'no:cacheprovider', '--junitxml=results.xml'], tmp, timeout=60)
            report['exit_code'] = result['exit_code']
            report['output'] = result['output'][-8000:]
            if result['timed_out']:
                raise RuntimeError('pytest_timeout')
            root = ET.parse(os.path.join(tmp, 'results.xml')).getroot()
            for case in root.iter('testcase'):
                state = 'failed' if case.find('failure') is not None else 'errors' if case.find('error') is not None else 'skipped' if case.find('skipped') is not None else 'passed'
                report[state] += 1
                report['suite'].append({'name': case.get('name'), 'status': state})
            total = sum(report[k] for k in ('passed', 'failed', 'errors', 'skipped'))
            report['pass_rate'] = round(report['passed'] / max(total, 1), 4)
            report['success'] = result['exit_code'] == 0 and report['passed'] == len(tests['cases']) and total == report['passed']
            if not report['success'] and not (report['failed'] or report['errors']):
                report.update(errors=1, error='incomplete_test_execution')
    except Exception as exc:
        report.update(errors=max(1, report['errors']), error=str(exc)[:1000], success=False)
    return finish()
