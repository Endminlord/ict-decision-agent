from runtime.advisory import advise


def risk_sentinel(risk, llm=None):
    if not isinstance(risk, dict):
        return {'risk_sentinel': {'level': 'unknown', 'stub': True}}
    findings = risk.get('findings', [])
    findings = findings if isinstance(findings, list) else []
    high = risk.get('verdict') == 'fail' or any(f.get('severity') in ('high', 'critical') for f in findings if isinstance(f, dict))
    level = 'high' if high else 'medium' if findings else 'low' if risk.get('scanned') else 'unknown'
    return {'risk_sentinel': {'level': level, 'findings': findings,
            'recommendations': ['block_execution_and_review'] if high else ['review_unscanned_inputs'] if level == 'unknown' else [],
            'advisory': advise(llm, '基于真实安全扫描结果提出风险处置建议。', risk), 'stub': not bool(risk)}}
