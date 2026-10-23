# 应用标志

本目录为微信图片发送工具的原创标志：蓝白色叠放相片与向右上方发送的箭头。没有使用微信、Google 或 Microsoft 的商标图形；配色不包含绿色。

## 使用文件

- `app-icon.png`：1254 × 1254 原始透明 PNG，供应用窗口和品牌展示使用。
- `app-icon.ico`：Windows 图标，内含 16、24、32、48、64、128、256 像素规格，供窗口和打包程序使用。
- `app-icon-32.png`、`app-icon-64.png`：小尺寸 PNG，供标题、状态栏等使用。

## 生成与处理

2026-10-22 使用 Codex 内置 `image_gen.imagegen` 工具生成，`transparent_background=true`。保留生成图像的真实 alpha 通道；仅使用 Pillow 进行等比例缩放和 ICO 格式封装，没有重绘、更换背景或去底。

## 原始提示词

```text
Use case: logo-brand
Asset type: original Windows desktop application icon for an image sending utility
Primary request: design one polished original app symbol, two neatly stacked landscape photo frames with a bold clean arrow sweeping toward the upper right, communicating efficient reliable image sending.
Style/medium: precise vector-like app icon with restrained light beveled depth reminiscent of refined Windows 7 desktop icons and the simplicity, proportions, and whitespace of modern Material design; original identity, no borrowed brand marks.
Composition/framing: single centered icon, square canvas, fill around 86 percent of the canvas with generous safe margins, clear strong silhouette readable at 16 and 32 pixels. Front photo frame in crisp white with deep royal blue edge; inside a simple blue mountain and small pale blue sun. Back frame offset subtly. The sending arrow is royal blue, clean and confident, visually connected to the frame.
Color palette: cobalt blue #2868CD, pale blue #B9D8F5, white #FFFFFF, steel gray #72839A only. No green, teal, yellow, orange or red.
Scene/backdrop: genuinely transparent alpha background, clean transparent space around the isolated icon, no opaque backdrop or checkered pattern.
Constraints: no text, no letters, no watermark, no mockup, no app window, no badge, no rounded-square tile surrounding the symbol, no excessive gloss or tiny details, no WeChat Google or Microsoft logos; preserve crisp opaque mark with smooth anti-aliased transparent edges.
```
