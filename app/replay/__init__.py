"""Historical replay / walk-forward simulation (V1, US only).

The production pipeline (event -> gate -> proposal -> paper order -> fill -> exit) runs UNCHANGED inside an isolated
Postgres schema of a separate replay database. Only three things differ from production:

  clock        app.core.clock.ReplayClock - every reused code path believes it is the simulated moment
  visibility   data is staged into the run's schema progressively: an event only once available_at <= now, a price
               bar only once its session is known, labels (supersession, reviews, delistings, returns) never early
  execution    prices come from the run's input snapshot through a capped provider (ReplayPriceProvider)

See docs/HISTORICAL_REPLAY.md."""
