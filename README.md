# Flip Loop

Two bot wallets that play Slotana PvP coin flips against each other.

Press **Start** and it runs a cycle right away, then every 12 hours, forever:

1. Wallet A creates a 0.005 SOL PvP flip
2. Wallet B joins it, Slotana settles it and pays the winner
3. Auto split: the richer wallet sends half the difference to the other, so both are equal again
4. Repeat 5 times (cooldowns between every step, each transaction confirmed before the next)

The dashboard shows both balances, the countdown to the next cycle, every game and split, and has an **Export private key** button for each wallet.

## Deploy on Railway

1. Create an empty repo on GitHub and upload all these files (Add file → Upload files).
2. Railway → New Project → Deploy from GitHub repo → pick the repo.
3. **Add a Volume** to the service (right-click the service → Attach volume, any mount path).
   The wallets are stored there. Without a volume they are deleted on every redeploy.
4. Service → Settings → Networking → Generate Domain. Open it.
5. Send at least 0.01 SOL to wallet A and wallet B (addresses are on the page).
6. Press **Start loop**.

Optional variables are listed in `.env.example` (bet size, schedule, password, simulation mode).

## Notes

- Keep **one** replica and one worker, otherwise two loops would run at once.
- If a join fails, the bot cancels A's game so the deposit is refunded.
- If the server restarts in the middle of a cycle, that cycle is marked INTERRUPTED; the next one runs on schedule.
- Run locally: `pip install -r requirements.txt` then `DRY_RUN=true python app.py` and open http://localhost:8080.
