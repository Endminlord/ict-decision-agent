from runtime.advisory import advise


def cost_tracker(usage, llm=None):
    if not isinstance(usage, dict):
        return {'cost_tracker': {'total_cost': None, 'stub': True, 'reason': 'invalid_usage'}}
    tokens = usage.get('tokens', usage)
    pricing = usage.get('pricing', {})
    complete = tokens.get('usage_complete', False)
    total = None
    reason = 'usage_incomplete' if not complete else 'pricing_not_configured'
    if complete and all(isinstance(pricing.get(k), (int, float)) and pricing[k] >= 0 for k in ('input_per_million', 'output_per_million')):
        total = round((tokens.get('prompt_tokens', 0) * pricing['input_per_million'] + tokens.get('completion_tokens', 0) * pricing['output_per_million']) / 1000000, 8)
        reason = None
    report = {'total_cost': total, 'currency': pricing.get('currency', 'USD'), 'tokens': tokens,
              'reason': reason, 'stub': False, 'basis': 'provider_usage_and_configured_prices'}
    if llm is not None:
        report['advisory'] = advise(llm, '分析 token 使用及成本优化建议。', usage)
    return {'cost_tracker': report}
