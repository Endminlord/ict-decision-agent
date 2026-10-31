# NEXENT ICT 观测台皮肤色板草稿

`tokens.css` 提供官方品牌色到语义变量的映射，供后续皮肤实现引用。

## 色板映射

| 主题 | Paper / 背景 | Ink / 前景 | Accent / 强调 |
| --- | --- | --- | --- |
| 浅色 | 纸白 `#eae5e1` | 暖黑 `#080a08` | 杏金 `#9b7247` |
| 暗色 | 暗纸 `#11181b` | 纸白 `#eae5e1` | 杏金 `#c5a16b` |

## 使用方式

- 默认 `:root` 为浅色主题。
- 在主题容器上设置 `data-theme="dark"` 可切换暗色变量。
- 使用 `--nexent-background` 设置页面背景，`--nexent-foreground` 设置主要文字，`--nexent-accent` 设置交互与数据强调色。
- 暗色主题同步更新 `--nexent-paper`、`--nexent-ink` 和 `--nexent-accent`，因此语义变量无需重复声明。
