# 车间正式 Logo · 01 过关

Kun 于 2026-11-18 确认「用 01 吧」，采用展示页 V2 的精修版。

图形是开放的工位与移出的工件，表达经过关口、完成交付。主标识用黑白，朱橙为可选应用配色，不属于图形识别的必要部分。

## 文件

- `workshop.svg`：标准版；透明底、currentColor，适用于内联 SVG。
- `workshop-black.svg` / `workshop-white.svg`：固定黑白，适用于外部图片引用与印刷。
- `workshop-small.svg`：16–32 px 使用的小尺寸版。
- `workshop-{black,white}-{16,24,32,64,256,1024}.png`：透明底 PNG；前三档使用小尺寸版，后三档使用标准版。
- `favicon.svg`：根据系统明暗模式切换颜色。
- `favicon.ico`：16、32、48、64 px，黑色透明底，供兼容用途。

## 使用

图形已带留白，不裁切，不拉伸，不添加阴影或渐变。深底使用白色版，浅底使用黑色版。和「车间」组合时使用系统无衬线字体；图形本身不依赖字体。

定稿前探索保留在 `../v1/`，精修过程保留在 `../refined/`，本目录是正式素材来源。状态页、盯更新日报/周报及 README 使用此标识。运行时素材在 `src/workshop/assets/`，随包安装并内嵌到离线 HTML。

设计流程参考 https://github.com/op7418/logo-generator-skill 。四种应用场景为本地矢量排版预览，未调用 Gemini 生成展示图。
