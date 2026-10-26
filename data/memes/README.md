# 表情包素材放这里

想让机器人发图，就把图片按「包」放好：

```
data/memes/
  我的沙雕猫/
    index.json      ← 可选，写关键词
    001.gif
    002.png
```

然后在控制台「设置 → 说话」里启用表情包，或者改 config.yaml：

```yaml
memes:
  enabled: true
  packs: ["我的沙雕猫"]
```

没有素材也没关系，默认是关的。图片版权自己负责，别放别人的商用表情。
