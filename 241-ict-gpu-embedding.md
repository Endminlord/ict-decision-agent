# 241 · embedding 推理 GPU 化实测（RTX 5070 + onnxruntime-gpu）

> 目的：`待补充清单.md` #29 里「GPU 推理（RTX 5070 + onnxruntime-gpu）未试」这一条，本次把它试完了。
>
> **一句话结论**：GPU 已**落地到 8792 服务**（2026-10-17）—— 端到端 **20.7 ms/batch8 = 387 条/秒**（CPU 基线 556 ms = 14.4 条/秒，**26.9 倍**）；纯推理环节是 13 ms / **44 倍**（§3），服务端多出的 7.7 ms 是 tokenize + HTTP + JSON 序列化。落地后政务/RFC 双侧评测读数**逐项零退化**（§4.3）。

---

## 1. 环境

| 项 | 值 |
|---|---|
| GPU | `NVIDIA GeForce RTX 5070 Laptop GPU` · 12,231 MiB · driver `616.56` |
| WSL CUDA 驱动 | `/usr/lib/wsl/lib/libcuda.so.1.1`（Windows 侧透传） |
| 模型 | bge-m3 ONNX：`/home/endmin/.cache/bge-m3-onnx/`（`model.onnx` 724,923 B + `model.onnx_data` 2,266,820,608 B） |
| 现有服务 | `bge-m3-embed.service`（systemd **user** unit，enabled + active，`--port 8792 --max-length 512`） |
| 现有 runtime | `onnxruntime 1.23.2`，providers = `['AzureExecutionProvider', 'CPUExecutionProvider']` ⇒ **纯 CPU** |

**关键前置事实**：`onnxruntime-gpu` 的 CUDA provider **只列出来不算数** —— 首次实测时报

```
Failed to load library libonnxruntime_providers_cuda.so with error:
  libcudnn.so.9: cannot open shared object file: No such file or directory
Failed to create CUDAExecutionProvider. Require cuDNN 9.* and CUDA 12.*.
```

且**静默回退到 CPU**（实际 provider 打印出来仍是 `['CPUExecutionProvider']`，速度 11.3 条/秒，**比纯 CPU 还慢**，因为白试了一次加载）。必须补 cuDNN 9。

---

## 2. 装上 CUDA provider 的完整步骤

```bash
# 1) 独立 venv（不动正在跑的服务）
uv venv /tmp/ort-gpu-test --python 3.10

# 2) ORT GPU 版 + 推理依赖
uv pip install --python /tmp/ort-gpu-test/bin/python \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  onnxruntime-gpu tokenizers numpy

# 3) cuDNN 9 与 cuBLAS 12 —— 走 PyPI 的 NVIDIA wheel，不必装系统 CUDA toolkit
uv pip install --python /tmp/ort-gpu-test/bin/python \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  nvidia-cudnn-cu12 nvidia-cublas-cu12

# 4) 关键一步：把 wheel 里的库目录加进 LD_LIBRARY_PATH（连同 WSL 的 libcuda）
NVLIBS=$(find /tmp/ort-gpu-test/lib/python3.10/site-packages/nvidia -maxdepth 2 -name lib -type d | tr '\n' ':')
LD_LIBRARY_PATH="${NVLIBS}/usr/lib/wsl/lib" /tmp/ort-gpu-test/bin/python /tmp/ort-gpu-bench.py
```

装上的版本：`onnxruntime-gpu 1.23.2` · `nvidia-cudnn-cu12 9.27.0.42` · `nvidia-cublas-cu12 12.9.2.10` · `nvidia-cuda-nvrtc-cu12 12.9.86`。

---

## 3. 实测读数（batch=8 · max_length=512 · 同一批文本）

| provider | 单批耗时 | 吞吐 | 实际生效 provider |
|---|---|---|---|
| `CUDAExecutionProvider` | **13 ms** | **635.0 条/秒** | `['CUDAExecutionProvider', 'CPUExecutionProvider']` ✓ |
| `CPUExecutionProvider` | 556 ms | 14.4 条/秒 | `['CPUExecutionProvider']` |
| （缺 cuDNN 时误配 CUDA） | 705 ms | 11.3 条/秒 | `['CPUExecutionProvider']`（回退，反而更慢） |

**加速比 44 倍**（635 ÷ 14.4）。

---

## 4. 落地到服务（**已执行 · 2026-10-17 09:34**）

前置条件当天满足：语料增量导入已结束（`ps` 无导入进程），切换不会中断任何任务。

### 4.1 实际步骤（与原计划的三处差异）

```bash
# 1) 装进服务自己的 venv（与原计划相同）
uv pip install --python /home/endmin/dsh/research/embed-svc/.venv/bin/python \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  onnxruntime-gpu nvidia-cudnn-cu12 nvidia-cublas-cu12
# → onnxruntime-gpu 1.23.2 · nvidia-cudnn-cu12 9.27.0.42
#   nvidia-cublas-cu12 12.9.2.10 · nvidia-cuda-nvrtc-cu12 12.9.86

# 2) 改代码：provider 从硬编码 CPU 改成环境变量（embed-server.py:40 附近）
#    want = [p.strip() for p in os.environ.get("EMBED_PROVIDERS", "CPUExecutionProvider").split(",") if p.strip()]
#    self.session = ort.InferenceSession(onnx_path, opts, providers=want)
#    print(f"[embed] providers requested={want} actual={self.session.get_providers()}", flush=True)
#    ← 必须打印 actual：CUDA 加载失败时 onnxruntime 会静默回退 CPU（§1 的坑），只看请求值会误判成功。

# 3) unit 加两个 Environment（EMBED_PROVIDERS + LD_LIBRARY_PATH，路径见下）
systemctl --user daemon-reload && systemctl --user restart bge-m3-embed.service
```

**原计划没写的坑**：`embed-server.py` 原本**没有 `import os`** —— 加上环境变量读取后首次启动直接 `NameError: name 'os' is not defined`，服务卡在 `activating` 状态。补 `import os` 后 5 秒内 active。

unit 里的两个 Environment：

```ini
Environment=EMBED_PROVIDERS=CUDAExecutionProvider,CPUExecutionProvider
Environment=LD_LIBRARY_PATH=/home/endmin/dsh/research/embed-svc/.venv/lib/python3.10/site-packages/nvidia/cudnn/lib:/home/endmin/dsh/research/embed-svc/.venv/lib/python3.10/site-packages/nvidia/cublas/lib:/home/endmin/dsh/research/embed-svc/.venv/lib/python3.10/site-packages/nvidia/cuda_nvrtc/lib:/usr/lib/wsl/lib
```

### 4.2 落地后实测

| 项 | 值 | 验证方式 |
|---|---|---|
| 实际生效 provider | `['CUDAExecutionProvider', 'CPUExecutionProvider']` | `/tmp/embed-svc.log` 的 `[embed] providers requested=… actual=…` |
| 显存占用 | 3,568 → **6,259 MiB**（+2,691） | `nvidia-smi` |
| 端到端 batch=8 | **20.7 ms = 387 条/秒** | 10 次请求取最优（含 tokenize + HTTP + JSON） |
| 端到端 batch=16 | 37.6 ms = **425 条/秒** | 同上 |
| 端到端 batch=32 | 77.4 ms = 413 条/秒（avg 455 ms，抖动大） | 同上 |
| 检索链路时延 | cross-doc-demo **0.375 s → 0.186 s** | 同一条查询切换前后各跑一次 |

**batch=32 的 avg 远大于 best**（455 ms vs 77 ms）：突发大批量下线程池/显存有抖动。**演示与评测用 batch≤16**。

### 4.3 落地后读数零退化（关键验收）

GPU 与 CPU 的浮点结果有 1e-4 级差异（同一查询最高分 0.8815 → 0.8816），所以必须重跑评测确认不是「更快但更不准」：

| 组 | 政务（GPU 后） | RFC（GPU 后，`ES_INDEX=rfc-1024`） |
|---|---|---|
| A 标题命中 | 0.6 / 0.69 / 0.7 / 0.71 / MRR 0.6418 | n=14 · 0.7857 / 0.9286 / 0.9286 / **1.0** / 0.869 |
| B 出处可回查 | 500/500 = 1.0 | 70/70 = 1.0 |
| C 领域外拒答 | 20/20 = 1.0 | 20/20 = 1.0 |
| C 领域内答出 | 10/10 = 1.0 | 15/15 = 1.0 |
| D 跨文档链 | 9/10 = 0.9 | **N/A**（`level_of` 对 RFC 返回「未知」，见 249 §12.3） |

**逐项与 CPU 版一致** ⇒ 换 provider 不影响判据。

### 4.4 回退

```bash
# 删掉 unit 里的 EMBED_PROVIDERS 行（或设为 CPUExecutionProvider）→ 重启即可，代码无需改动
systemctl --user daemon-reload && systemctl --user restart bge-m3-embed.service
# 备份：embed-server.py.bak-before-gpu-20261017-093408
#       ~/.config/systemd/user/bge-m3-embed.service.bak-before-gpu-20261017-093408
```

**验收判据（保留原始）**：重启后 `/health` 正常，且实测单批吞吐从 ~14 条/秒升到 **数百条/秒**（只列 provider 不算通过，见 §1 的坑）。

---

## 5. 对 #29 的影响（**不能外推的部分**）

- embedding **推理本身**：14.4 → 635 条/秒（44×）。
- **端到端 embedding 已实测（2026-10-17 09:34 切换后）**：batch=8 **387 条/秒**（CPU 基线 14.4 ⇒ **26.9×**）；检索链路 `cross-doc-demo.py` 单次查询 **0.375 → 0.186 秒**。
- **端到端导入已于 2026-10-18 重测**：**2026-10-18 GPU 落地后重测（本次）**：200 块 batch20 **3.38 块/秒** · 1,000 块 batch50 **8.91 块/秒** · 3,000 块 batch100 **30.75 块/秒** · 3,000 块（offset 3000 全新内容，排除缓存）**32.74 块/秒** · 1,200 块 batch200 **35.57 块/秒** · batch400 回落到 32.28 块/秒。**吞吐主要由 batch 决定**（每批固定开销约 3 秒），batch 100–200 是甜点。**全量 1,864,451 块外推约 15–16 小时**，不再是 CPU 时代的 27–43 天。口径：1,200–3,000 块样本外推，大规模 ES segment merge 可能有非线性；单进程单机。
- **原结论「全量导入不可行」已失效** —— 但**不要外推成「随时可导」**：15–16 小时是单次窗口，期间 embedding 服务与 ES 都被占满，且这是子集选型之外的额外工作。

---

## 6. 诚实边界

1. **已落地（2026-10-17 09:34）**：`bge-m3-embed` 服务已切 CUDA —— `actual=['CUDAExecutionProvider', 'CPUExecutionProvider']`、显存 3,568 → **6,259 MiB**。步骤与回退见 §4。
2. **端到端已测**：batch=8 **20.7 ms = 387 条/秒**（CPU 14.4 条/秒 ⇒ **26.9×**），batch=16 425 条/秒。见 §5。
3. **显存已记录，压力未系统测**：batch=8 时 6,259 MiB；batch=32 延迟抖动大（avg 455 ms），更大 batch 或更长序列是否 OOM 未试 ⇒ 演示用 batch ≤ 16。
4. **未测并发**：服务是 `ThreadingHTTPServer`，多线程同时打 GPU 的行为未测。
5. 基准用的是**同一句文本重复 8 次**，真实语料长度分布不同，吞吐会低于 635 条/秒。

---

## 7. 复跑命令

```bash
# 基准脚本（CPU vs CUDA 对照）
NVLIBS=$(find /tmp/ort-gpu-test/lib/python3.10/site-packages/nvidia -maxdepth 2 -name lib -type d | tr '\n' ':')
LD_LIBRARY_PATH="${NVLIBS}/usr/lib/wsl/lib" /tmp/ort-gpu-test/bin/python /tmp/ort-gpu-bench.py
```

**预期读数**：`CUDA OK 13 ms / batch8 → 635.0 条/秒`，且实际 provider 含 `CUDAExecutionProvider`；`CPU OK 556 ms / batch8 → 14.4 条/秒`。
