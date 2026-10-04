# AI 生成 vs 真实在售 —— 标题对比报告

生成时间：2026-10-04T20:21:58

> **真实标题 = ground truth。** 它是运营真金白银在跑的文案，
> 信息顺序、卖点取舍、措辞风格都被市场检验过。
> 亚马逊正文（五点描述）抓不到，所以这里只对比标题——但标题恰恰是权重最高的那一段。

## 总览

| SKU | 站点 | 真实长度 | AI长度 | 实词覆盖率 | 漏掉的实词 | 结构差异 |
|---|---|---|---|---|---|---|
| S02M1225B | DE | 182 | 165 | 59% | heim、kabelmanagement、max、rollbar、serie、standfuss | 无 |
| B08QJ5C3BT | DE | 189 | 175 | 62% | ablagen、aus、design、kabelmanagement、max、moderner | 无 |
| B0BHW3STTC | US | 142 | 147 | 69% | column、concealed、double、shelf、tall | 无 |

## 逐条对照

### S02M1225B · DE（基准：Amazon.de）

**真实在售标题**（182 字符，9 段）

> FITUEYES TV Ständer Rollbar für 24–48 Zoll, bis zu 35kg, Master Serie | TV Standfuss, VESA Max. 200x200mm, Höhenverstellbar, mit Rollen & Kabelmanagement, für Wohnung Büro Heim, Weiß

**AI 生成标题**（165 字符，6 段）

> FITUEYES TV Ständer mit Rollen für 24-48 Zoll, bis 35 kg, VESA 75x75 bis 200x200 mm, Master V2 FT48, höhenverstellbarer TV Rollwagen für Wohnzimmer und Büro, Schwarz

- 实词覆盖率：**59%**
- AI 没写到真实标题里的这些词：['heim', 'kabelmanagement', 'max', 'rollbar', 'serie', 'standfuss', 'weiß', 'wohnung', 'zu']

来源：https://www.amazon.de/dp/B0CMZMBKG5/

### B08QJ5C3BT · DE（基准：Amazon.de）

**真实在售标题**（189 字符，1 段）

> FITUEYES Design TV Ständer 75-100 Zoll Moderner TV Stand mit Ablagen Kabelmanagement aus Holz und Metall TV Standfuss Schwenkbar Höhenverstellbar bis zu 85kg Max.VESA 800 * 600 Eiffel Serie

**AI 生成标题**（175 字符，9 段）

> FITUEYES TV Ständer mit Halterung für 75-100 Zoll, 85 kg, VESA bis 800x600 mm, Eiffel Serie | schwenkbar, höhenverstellbar, Ablagefächer | Wohnzimmer und Büro, Holz und Metall

- 实词覆盖率：**62%**
- AI 没写到真实标题里的这些词：['ablagen', 'aus', 'design', 'kabelmanagement', 'max', 'moderner', 'stand', 'standfuss', 'zu']

来源：https://www.amazon.de/dp/B08QJ5C3BT/

### B0BHW3STTC · US（基准：Amazon.ae）

**真实在售标题**（142 字符，5 段）

> FITUEYES TV Floor Stand for 32-75 inch, Universal Swivel TV Stand, Tall TV Stand with Concealed Storage, Double Column Adjustable Shelf, White

**AI 生成标题**（147 字符，4 段）

> FITUEYES Eiffel Series 32-75 inch Floor TV Stand with Storage, Universal Swivel TV Media Stand for Living Room and Office, Adjustable Height, White

- 实词覆盖率：**69%**
- AI 没写到真实标题里的这些词：['column', 'concealed', 'double', 'shelf', 'tall']

来源：https://www.amazon.ae/dp/B0BHW3STTC/

---

## 怎么用这份报告

**不要念数字，要讲结论和下一步**：

- 「我拿 AI 生成的标题和你们真实在售的标题做了对比。实词覆盖率 X%——
  说明大方向对了，但漏掉了 A、B 这两个卖点。」
- 「真实标题用了 `|` 分隔卖点，AI 输出的是逗号。**这是目标市场的表达习惯问题**，
  可以写进 Prompt 的站点规范里。」
- 「真实标题把承重写在靠前的位置，说明这是买家最关心的参数之一；
  我现在的 Prompt 没有规定信息的优先级顺序，可以补上。」

> ⚠️ **一个必须说清楚的局限**：只对比了 1 条真实标题，
> 单个样本说明不了市场规律。要得出结论至少要 10-20 条。
> 主动说出这一点，比假装有统计意义要安全得多。