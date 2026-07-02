# Next Steps

## Immediate

1. Decide the exact trading instrument set.
2. Decide the timeframe for strategy execution.
3. Write the user's actual entry and exit rules in plain language.
4. Convert those rules into code/config.
5. Run paper mode long enough to build candle history.

## Recommended First Target

Choose one of these before coding the custom strategy:

- NIFTY options
- BANKNIFTY options
- Cash equities

## Commands

Refresh token:

```powershell
C:\AlgoTrader\.venv\Scripts\python.exe C:\AlgoTrader\refresh_token.py
```

Run one paper cycle:

```powershell
C:\AlgoTrader\.venv\Scripts\python.exe C:\AlgoTrader\main.py --config C:\AlgoTrader\config\strategy.example.json --mode paper --once
```

Run continuous paper mode:

```powershell
C:\AlgoTrader\.venv\Scripts\python.exe C:\AlgoTrader\main.py --config C:\AlgoTrader\config\strategy.example.json --mode paper
```

Run tests:

```powershell
C:\AlgoTrader\.venv\Scripts\python.exe -m unittest discover -s C:\AlgoTrader\tests
```

## Practical Note

With `5minute` candles and local candle building, warm-up takes time. For fast
testing, consider temporarily switching the config interval to `1minute`.
