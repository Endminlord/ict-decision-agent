from runtime.advisory import advise


def cache_governor(data, llm=None):
    if not isinstance(data, dict):
        return {'cache_governor': {'policy': 'disabled', 'ttl': 0, 'stub': True}}
    return {'cache_governor': {'policy': 'ttl', 'ttl': max(0, int(data.get('ttl', 3600))),
            'observed': data, 'applied': False, 'stub': not bool(data),
            'advisory': advise(llm, '根据真实缓存命中和请求情况提出策略建议；不宣称已应用配置。', data)}}
