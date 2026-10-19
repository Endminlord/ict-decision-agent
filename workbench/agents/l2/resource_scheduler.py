"""Deterministic capacity-bounded DAG plan; optional LLM advice only."""
from runtime.advisory import advise


def resource_scheduler(resources, llm=None):
    report = {'schedule': [], 'waves': [], 'stub': False, 'success': False, 'executed': False}
    try:
        if not isinstance(resources, dict) or not resources:
            raise ValueError('missing_resources')
        dag = resources.get('task_dag', {})
        stages = dag.get('stages', [])
        edges = dag.get('edges', [])
        if not isinstance(stages, list) or not stages or any(not isinstance(s, str) or not s for s in stages) or len(set(stages)) != len(stages):
            raise ValueError('invalid_stages')
        deps = {s: set() for s in stages}
        for edge in edges:
            if not isinstance(edge, (list, tuple)) or len(edge) != 2 or any(x not in deps for x in edge):
                raise ValueError('invalid_dependency')
            deps[edge[1]].add(edge[0])
        capacity = min(int(resources.get('cpu_count', 1)), int(resources.get('max_workers', 1)))
        memory = resources.get('available_memory_mb')
        if memory is not None:
            capacity = min(capacity, int(memory) // max(1, int(resources.get('memory_per_worker_mb', 256))))
        if capacity < 1:
            raise ValueError('insufficient_resources')
        if resources.get('budget_remaining') is not None and resources['budget_remaining'] <= 0:
            raise ValueError('budget_exhausted')
        # Validate the entire DAG even if some stages were completed.
        done, waves = set(), []
        while len(done) < len(stages):
            ready = [s for s in stages if s not in done and deps[s] <= done]
            if not ready:
                raise ValueError('dependency_cycle')
            wave = ready[:capacity]
            waves.append(wave)
            done.update(wave)
        completed = set(resources.get('completed_stages', []))
        waves = [[s for s in wave if s not in completed] for wave in waves]
        waves = [wave for wave in waves if wave]
        report.update(schedule=[s for wave in waves for s in wave], waves=waves, max_workers=capacity,
                      success=True, basis='dependency_graph_and_observed_capacity')
        report['advisory'] = advise(llm, '分析资源和有向无环任务图，提出可选调度建议；不可替代确定性计划。', resources)
    except (ValueError, TypeError, KeyError) as exc:
        report.update(error=str(exc), stub=not bool(resources))
    return {'resource_scheduler': report}
