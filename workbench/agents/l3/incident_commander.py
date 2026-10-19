from runtime.advisory import advise


def incident_commander(incident, llm=None):
    if not isinstance(incident, dict):
        return {'incident_commander': {'status': 'unknown', 'stub': True}}
    errors = incident.get('errors', [])
    critical = incident.get('level') == 'critical' or incident.get('security', {}).get('verdict') == 'fail'
    return {'incident_commander': {'status': 'escalated' if critical else 'investigating' if errors else 'no_incident',
            'severity': 'critical' if critical else 'medium' if errors else 'none',
            'action': 'request_human_review' if critical else 'inspect_errors' if errors else 'none',
            'errors': errors, 'advisory': advise(llm, '基于真实错误和熔断记录提出事故处置建议。', incident),
            'stub': not bool(incident)}}
