from runtime.advisory import advise


def observability_agent(metrics, llm=None):
    if not isinstance(metrics, dict):
        return {'observability_agent': {'dashboard': {}, 'alerts': ['invalid_metrics'], 'stub': True}}
    alerts = []
    if metrics.get('errors'):
        alerts.append('stage_errors')
    if metrics.get('fuse', {}).get('retry_count', 0):
        alerts.append('retries_observed')
    execution = metrics.get('execution', {})
    if execution and not execution.get('success', False):
        alerts.append('execution_not_passed')
    report = {'dashboard': {'status': 'warning' if alerts else 'ok', 'metrics': metrics},
              'alerts': alerts, 'stub': not bool(metrics)}
    report['advisory'] = advise(llm, '根据实际阶段日志、熔断及执行结果提出监控建议。', metrics)
    return {'observability_agent': report}
