"""Per-run measurements; never estimate tokens from input string length."""
import time


class BudgetExceeded(RuntimeError):
    pass


class MeteredLLM:
    def __init__(self, client, policy=None, workflow_id=None):
        self.client = client
        self.policy = policy
        # 工作流级预算的落点：同一个 policy 可以被多个工作流共用，记账要分开。
        self.workflow_id = workflow_id
        self.calls = []
        self.budget_exhausted = False

    def __getattr__(self, name):
        """Proxy unknown attributes to the wrapped client — except dunders.

        Proxying dunders is not harmless. ``inspect.signature(instance)`` reads
        ``__signature__``; proxying it made callers see the *inner* function's
        signature ``(prompt, model=None)``, so they passed two arguments to
        ``__call__``, which accepted one. The resulting TypeError was raised
        before ``chat`` — outside the metering block — so every Agent silently
        fell back to a deterministic stub and the run reported zero model calls
        no matter how the provider was configured.
        """
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self.client, name)

    def chat(self, messages, model=None, **kwargs):
        if self.policy and not self.policy.check_budget(1, self.workflow_id):
            self.budget_exhausted = True
            raise BudgetExceeded('llm_call_budget_exceeded')
        started = time.monotonic()
        record = {'success': False, 'usage': None}
        try:
            # The wrapped client may be an object with .chat(), or a plain
            # callable(prompt, model=None) as run.py's live path builds.
            if hasattr(self.client, "chat"):
                result = self.client.chat(messages, **kwargs)
            elif model is not None:
                result = self.client(messages, model)
            else:
                result = self.client(messages)
            if not isinstance(result, dict):
                result = {'content': result, 'usage': None}
            usage = result.get('usage')
            # 推理 token 有时在响应的**顶层**而不在 usage 里 —— 本项目的 LLMClient
            # 就是这么返回的（usage 走 _normalize_usage，推理数单列在顶层，因为
            # 网关把它同时放在 completion_tokens_details 和 completion_thinking_tokens
            # 两处，归一化时谁都不认）。不在这里折进来，快照就永远读到 0 ——
            # 而它是「出口实际挂了哪一档」唯一拿得到的证据。
            if isinstance(usage, dict) and 'reasoning_tokens' not in usage:
                reported = result.get('reasoning_tokens')
                if isinstance(reported, int) and reported >= 0:
                    usage = {**usage, 'reasoning_tokens': reported}
            record.update(success=True, usage=usage)
            if self.policy is not None:
                # Charge the observed usage. The spend already happened, so this
                # cannot fail; crossing a limit makes the NEXT request refused.
                self.policy.charge(result.get('usage'), self.workflow_id)
                if self.policy.exhausted_reason is not None:
                    self.budget_exhausted = True
            return result
        except Exception as exc:
            record['error_type'] = type(exc).__name__
            raise
        finally:
            record['elapsed_ms'] = round((time.monotonic() - started) * 1000, 2)
            self.calls.append(record)

    def snapshot(self):
        prompt = completion = reasoning = measured = reasoning_reported = 0
        failures: dict = {}
        for call in self.calls:
            usage = call.get('usage')
            if isinstance(usage, dict) and all(isinstance(usage.get(k), int) and usage[k] >= 0 for k in ('prompt_tokens', 'completion_tokens')):
                measured += 1
                prompt += usage['prompt_tokens']
                completion += usage['completion_tokens']
            # 推理 token 单记一份，**不加进 total**：思考与正文共享同一份输出预算，
            # completion_tokens 里已经含了它，再加一遍就是重复计数。
            #
            # 它是「出口实际挂的档位」唯一能拿到的证据：本项目造 payload 时只写
            # model，从不发档位参数（档位挂在出口那边），所以想知道上一批任务烧了
            # 多少预算在思考上，只能读响应。`reasoning_reported_calls` 用来区分
            # 「没在思考」和「这个出口根本不报」——0 个 token 配 0 个 reported
            # 说明没人报，不是没想。
            thinking = usage.get('reasoning_tokens') if isinstance(usage, dict) else None
            if isinstance(thinking, int) and thinking >= 0:
                reasoning_reported += 1
                reasoning += thinking
            if not call.get('success'):
                # `failed_calls` 是一个数，但**是哪种失败**没人留下来。live run
                # a3da7ca09aca 报了 22 次调用 14 次失败、链子却照样跑完并通过 ——
                # 光看这个数分不清是网关 429、是熔断挡住了调用、还是响应少了
                # choices[0]，而这三样的处置完全不同。分类计数：不改失败本身的
                # 行为，只让它在结果里说得出来。
                kind = call.get('error_type') or 'Unknown'
                failures[kind] = failures.get(kind, 0) + 1
        snapshot = {'calls': len(self.calls), 'successful_calls': sum(c['success'] for c in self.calls),
                'failed_calls': sum(not c['success'] for c in self.calls), 'measured_calls': measured,
                'unmeasured_calls': len(self.calls) - measured, 'prompt_tokens': prompt,
                'completion_tokens': completion, 'total_tokens': prompt + completion,
                'reasoning_tokens': reasoning, 'reasoning_reported_calls': reasoning_reported,
                'usage_complete': measured == len(self.calls),
                'elapsed_ms': round(sum(c['elapsed_ms'] for c in self.calls), 2),
                'budget_exhausted': self.budget_exhausted}
        if failures:
            snapshot['failed_call_types'] = failures
        return snapshot

    def __call__(self, messages, model=None, **kwargs):
        # A second positional is accepted because callers legitimately probe for
        # it; see the note on __getattr__.
        return self.chat(messages, model=model, **kwargs)
