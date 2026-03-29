# 📋 Strategy / Risk Config Guide — bot1

## ภาพรวมโปรเจค

Bot นี้คือ **Binance Futures Trading Bot** (Python) ที่ทำงานแบบ loop ทุก N วินาที ซื้อขาย Futures ด้วย Strategy 3 แบบ:

| Strategy | ใช้กับ | แนวคิด |
|---|---|---|
| `EMA_CROSS` | BTC, ETH, BNB | Trend Following — ตามเทรนด์ |
| `RSI_BOLLINGER` | SOL, SUI, AVAX | Mean Reversion — ซื้อ dip, ขาย top |
| `BREAKOUT_VOLUME` | TAO, RENDER, DOGE | Momentum — ตาม breakout + volume spike |

---

## 🔢 Parameter ใน [.env](file:///Users/autapankiawoon/Documents/bot/bot1/.env) ทั้งหมด (Strategy / Risk Config)

| Parameter | ความหมาย | Default |
|---|---|---|
| `LEVERAGE` | เลเวอเรจ (x เท่า) | `5` |
| `RISK_PER_TRADE_PCT` | % ของ Balance ที่ยอมเสียต่อไม้ | `1` |
| `TAKE_PROFIT_PCT` | % กำไรที่จะปิดไม้ (จาก entry) | `2` |
| `STOP_LOSS_PCT` | % ขาดทุนที่จะปิดไม้ (จาก entry) | `1` |
| `TRADE_COOLDOWN_MINUTES` | ระยะเวลาพักหลังปิดไม้ (นาที) | `60` |
| `MAX_CONCURRENT_POSITIONS` | จำนวน position พร้อมกันสูงสุด | `5` |
| `DAILY_DRAWDOWN_LIMIT_PCT` | % ขาดทุนสะสมต่อวันสูงสุด | `5` |
| `ATR_PERIOD` | Period ของ ATR indicator | `14` |
| `ATR_STOP_MULTIPLIER` | ตัวคูณ ATR สำหรับ stop loss | `1.5` |

> **หมายเหตุ:** `TRADE_COOLDOWN_MINUTES`, `MAX_CONCURRENT_POSITIONS`, `DAILY_DRAWDOWN_LIMIT_PCT`, `ATR_PERIOD`, `ATR_STOP_MULTIPLIER` มีอยู่ใน [.env.example](file:///Users/autapankiawoon/Documents/bot/bot1/.env.example) แต่ bot ปัจจุบันยังไม่ได้ใช้งาน — logic อยู่ที่ `LEVERAGE`, `RISK_PER_TRADE_PCT`, `TAKE_PROFIT_PCT`, `STOP_LOSS_PCT` เป็นหลัก

---

## 🎚️ Preset ความเสี่ยง 3 ระดับ

### 🟢 ความเสี่ยงต่ำ (Conservative)

เน้นรักษาทุน เหมาะสำหรับมือใหม่ หรือช่วงตลาดผันผวนสูง

```env
# Strategy / risk config — LOW RISK
LEVERAGE=2
RISK_PER_TRADE_PCT=0.5
TAKE_PROFIT_PCT=1.5
STOP_LOSS_PCT=0.75
TRADE_COOLDOWN_MINUTES=120
MAX_CONCURRENT_POSITIONS=3
DAILY_DRAWDOWN_LIMIT_PCT=3
ATR_PERIOD=14
ATR_STOP_MULTIPLIER=1.0
```

| ผล | ค่า |
|---|---|
| Risk:Reward | 1 : 2 |
| ขาดทุนสูงสุดต่อไม้ | 0.5% ของ Balance |
| Drawdown สูงสุด/วัน | 3% ของ Balance |

---

### 🟡 ความเสี่ยงกลาง (Balanced) — **Default ปัจจุบัน**

สมดุลระหว่างกำไรและความเสี่ยง เหมาะสำหรับทดสอบจริง

```env
# Strategy / risk config — MEDIUM RISK
LEVERAGE=5
RISK_PER_TRADE_PCT=1.0
TAKE_PROFIT_PCT=2.0
STOP_LOSS_PCT=1.0
TRADE_COOLDOWN_MINUTES=60
MAX_CONCURRENT_POSITIONS=5
DAILY_DRAWDOWN_LIMIT_PCT=5
ATR_PERIOD=14
ATR_STOP_MULTIPLIER=1.5
```

| ผล | ค่า |
|---|---|
| Risk:Reward | 1 : 2 |
| ขาดทุนสูงสุดต่อไม้ | 1% ของ Balance |
| Drawdown สูงสุด/วัน | 5% ของ Balance |

---

### 🔴 ความเสี่ยงสูง (Aggressive)

เพิ่มโอกาสกำไรแต่ขาดทุนได้มากขึ้น เหมาะกับผู้มีประสบการณ์และยอมรับความเสี่ยงสูง

```env
# Strategy / risk config — HIGH RISK
LEVERAGE=10
RISK_PER_TRADE_PCT=2.0
TAKE_PROFIT_PCT=4.0
STOP_LOSS_PCT=2.0
TRADE_COOLDOWN_MINUTES=30
MAX_CONCURRENT_POSITIONS=9
DAILY_DRAWDOWN_LIMIT_PCT=10
ATR_PERIOD=14
ATR_STOP_MULTIPLIER=2.0
```

| ผล | ค่า |
|---|---|
| Risk:Reward | 1 : 2 |
| ขาดทุนสูงสุดต่อไม้ | 2% ของ Balance |
| Drawdown สูงสุด/วัน | 10% ของ Balance |

---

## 📐 สูตรคำนวณ Position Size (จาก [risk.py](file:///Users/autapankiawoon/Documents/bot/bot1/src/trading/risk.py))

```
max_loss_usdt  = balance × (RISK_PER_TRADE_PCT / 100)
notional_usdt  = max_loss_usdt / (STOP_LOSS_PCT / 100)
margin_usdt    = notional_usdt / LEVERAGE
quantity       = notional_usdt / entry_price
```

**ตัวอย่าง** (Medium Risk, Balance = 1,000 USDT, BTC = 90,000 USDT):

```
max_loss  = 1000 × 0.01 = 10 USDT
notional  = 10 / 0.01   = 1,000 USDT
margin    = 1000 / 5    = 200 USDT  ← เงินที่วาง
quantity  = 1000 / 90000 ≈ 0.011 BTC
```

---

## 🎯 เงื่อนไขการเข้าไม้ (Entry Signal) แต่ละ Strategy

### 1. `EMA_CROSS` — สำหรับ BTC, ETH, BNB

**Logic:** EMA50 ตัด EMA200 (Golden/Death Cross)

| ทิศทาง | เงื่อนไข |
|---|---|
| **Long** ✅ | EMA50 ตัดขึ้นเหนือ EMA200 **และ** ราคาปิดอยู่เหนือ EMA200 |
| **Short** ✅ | EMA50 ตัดลงต่ำกว่า EMA200 **และ** ราคาปิดอยู่ต่ำกว่า EMA200 |

**เปลี่ยน parameter ใน [strategies_config.py](file:///Users/autapankiawoon/Documents/bot/bot1/src/config/strategies_config.py):**
```python
"params": {"fast_ema": 50, "slow_ema": 200}
# ลด fast_ema = เข้าไม้บ่อยขึ้น (เช่น 20/50)
# เพิ่ม slow_ema = เทรนด์ระยะยาวขึ้น (เช่น 50/200)
# เพิ่ม min_spread_pct = กรองสัญญาณอ่อน (เช่น 0.5)
```

---

### 2. `RSI_BOLLINGER` — สำหรับ SOL, SUI, AVAX

**Logic:** ซื้อ dip + ขาย top ด้วย RSI และ Bollinger Bands

| ทิศทาง | เงื่อนไข |
|---|---|
| **Long** ✅ | ราคา ≤ Bollinger Lower Band **และ** RSI ≤ 30 (Oversold) |
| **Short** ✅ | ราคา ≥ Bollinger Upper Band **และ** RSI ≥ 70 (Overbought) |

**เปลี่ยน parameter ใน [strategies_config.py](file:///Users/autapankiawoon/Documents/bot/bot1/src/config/strategies_config.py):**
```python
"params": {
    "rsi_period": 14,
    "buy_level": 30,   # ลดลง (เช่น 25) = เข้า long เฉพาะ oversold มากๆ
    "sell_level": 70,  # เพิ่มขึ้น (เช่น 75) = เข้า short เฉพาะ overbought มากๆ
    "bb_period": 20,
    "bb_std": 2,       # เพิ่มเป็น 2.5 = Bollinger กว้างขึ้น, เข้าไม้น้อยลง
}
```

---

### 3. `BREAKOUT_VOLUME` — สำหรับ TAO, RENDER, DOGE

**Logic:** Breakout เหนือ/ต่ำกว่า High/Low 20 แท่งก่อนหน้า พร้อม Volume spike

| ทิศทาง | เงื่อนไข |
|---|---|
| **Long** ✅ | ราคา > High สูงสุดใน 20 แท่งก่อน **และ** Volume ≥ 1.5× Volume MA20 |
| **Short** ✅ | ราคา < Low ต่ำสุดใน 20 แท่งก่อน **และ** Volume ≥ 1.5× Volume MA20 |

**เปลี่ยน parameter ใน [strategies_config.py](file:///Users/autapankiawoon/Documents/bot/bot1/src/config/strategies_config.py):**
```python
"params": {
    "breakout_lookback": 20,    # เพิ่มขึ้น = ต้อง break High/Low ยาวขึ้น
    "volume_ma_period": 20,
    "volume_multiplier": 1.5,   # เพิ่มขึ้น (เช่น 2.0) = ต้องการ volume spike แรงขึ้น
    "allow_short": True,         # False = เล่น Long อย่างเดียว (ปลอดภัยกว่า)
}
```

---

## ⚙️ Parameter เปรียบเทียบ 3 ระดับ (ตาราง)

| Parameter | 🟢 ต่ำ | 🟡 กลาง | 🔴 สูง |
|---|---|---|---|
| LEVERAGE | 2x | 5x | 10x |
| RISK_PER_TRADE_PCT | 0.5% | 1.0% | 2.0% |
| TAKE_PROFIT_PCT | 1.5% | 2.0% | 4.0% |
| STOP_LOSS_PCT | 0.75% | 1.0% | 2.0% |
| MAX_CONCURRENT_POSITIONS | 3 | 5 | 9 |
| DAILY_DRAWDOWN_LIMIT_PCT | 3% | 5% | 10% |
| Risk:Reward ratio | 1:2 | 1:2 | 1:2 |

> [!TIP]
> บอทปัจจุบันรัน **Testnet** เสมอ (`testnet = True` ใน [main.py](file:///Users/autapankiawoon/Documents/bot/bot1/main.py) line 134) ก่อนย้ายไป Mainnet ต้องแก้ไขบรรทัดนั้น

> [!IMPORTANT]
> `TRADE_COOLDOWN_MINUTES`, `MAX_CONCURRENT_POSITIONS`, `DAILY_DRAWDOWN_LIMIT_PCT` มีใน [.env](file:///Users/autapankiawoon/Documents/bot/bot1/.env) แต่ **ยังไม่ได้ implement** ใน code — ต้องเพิ่ม logic เองหากต้องการใช้จริง
