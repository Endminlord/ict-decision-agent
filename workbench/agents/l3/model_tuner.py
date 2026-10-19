from runtime.advisory import advise


def model_tuner(model, llm=None):
    if not isinstance(model, dict):
        return {'model_tuner': {'config': {}, 'stub': True}}
    return {'model_tuner': {'config': model.get('config', {}), 'usage': model.get('usage', {}),
            'applied': False, 'recommendations': [], 'stub': not bool(model),
            'advisory': advise(llm, '基于实际模型配置、延迟和用量提出优化建议，不声称已经调参。', model)}}
