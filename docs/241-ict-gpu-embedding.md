# 241 · embedding 推理 GPU 化实测（RTX 5070 + onnxruntime-gpu）

> 目的：`待补充清单.md` #29 里「GPU 推理（RTX 5070 + onnxruntime-gpu）未试」这一条，本次把它试完了。
>
> **一句话结论**：GPU 可用且**快 44 倍**（batch=8：CPU 556 ms → CUDA 13 ms），但**尚未落地到正在跑的服务**（当前 8792 仍是 CPU），落地脚本与步骤见 §4。

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

## 4. 落地到服务（**尚未执行**，等当前导入跑完）

当前 8792 服务**仍在 CPU 上跑**，且正被语料增量导入占用（`待补充清单.md` #29）。**切换会中断导入**，因此按下面步骤等导入结束再做：

```bash
# 1) 把 GPU 版装进服务自己的 venv
SVC_VENV=/home/endmin/dsh/research/embed-svc/.venv
uv pip install --python $SVC_VENV/bin/python \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  onnxruntime-gpu nvidia-cudnn-cu12 nvidia-cublas-cu12

# 2) 给 systemd user unit 加库路径（编辑 ~/.config/systemd/user/bge-m3-embed.service）
#    [Service] 段追加：
#    Environment=LD_LIBRARY_PATH=/home/endmin/dsh/research/embed-svc/.venv/lib/python3.10/site-packages/nvidia/cudnn/lib:/home/endmin/dsh/research/embed-svc/.venv/lib/python3.10/site-packages/nvidia/cublas/lib:/home/endmin/dsh/research/embed-svc/.venv/lib/python3.10/site-packages/nvidia/cuda_nvrtc/lib:/usr/lib/wsl/lib

systemctl --user daemon-reload
systemctl --user restart bge-m3-embed.service

# 3) 验证（两条都要看）
systemctl --user status bge-m3-embed.service --no-pager | head -6
curl -s http://127.0.0.1:8792/health
#    并且实际跑一次嵌入，确认速度而非仅看 provider 列表
```

**验收判据**：重启后 `/health` 正常，且实测单批吞吐从 ~14 条/秒升到 **数百条/秒**（只列 provider 不算通过，见 §1 的坑）。

---

## 5. 对 #29 的影响（**不能外推的部分**）

- embedding **推理本身**：14.4 → 635 条/秒（44×）。
- **端到端导入速度不一定是 44×** —— 当前实测 0.96 块/秒的瓶颈可能同时在 ES 写入、HTTP 往返、以及服务端串行批处理上。**未做端到端 GPU 化实测**，因此**不宣称**「全量 1,864,451 块从 27 天降到 X 天」。
- 若要拿到端到端数字，需在切换后重跑一小段导入（例如 1,000 块）并计时。

---

## 6. 诚实边界

1. **未落地**：本次只做到「独立 venv 里实测通过」，**正在跑的服务仍是 CPU**。落地步骤见 §4，尚未执行。
2. **未测端到端**：见 §5。
3. **未测显存压力**：bge-m3 在 batch=8 / max_length=512 下的显存占用未记录；更大 batch 或更长序列是否 OOM 未试。
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
