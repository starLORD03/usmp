# Changelog

## [0.2.0](https://github.com/starLORD03/usmp/compare/v0.1.0...v0.2.0) (2026-11-28)


### Features

* **calendar:** warn about events that end before they start ([#58](https://github.com/starLORD03/usmp/issues/58)) ([476cbd6](https://github.com/starLORD03/usmp/commit/476cbd675f6a75df9de8cc7bbd1c56dad8d4ec48))
* **core:** move only a date range or chosen calendars ([#55](https://github.com/starLORD03/usmp/issues/55)) ([19e4360](https://github.com/starLORD03/usmp/commit/19e43606310ddbada79b4540cee13eb5e88862cc))
* **core:** safe re-runs and resume after interruption ([#52](https://github.com/starLORD03/usmp/issues/52)) ([0b72fdd](https://github.com/starLORD03/usmp/commit/0b72fdd498cdbdcfc6569a46a3286751d268e411))
* **sdk:** retries, rate limits and faster writes ([#56](https://github.com/starLORD03/usmp/issues/56)) ([8c6629d](https://github.com/starLORD03/usmp/commit/8c6629deb8558042c753ceabbbe760059158fce0))


### Bug Fixes

* **calendar:** import invitations into Google and keep their location ([#57](https://github.com/starLORD03/usmp/issues/57)) ([bc747e5](https://github.com/starLORD03/usmp/commit/bc747e5511c203b2fa4e8fd001bd31e2d3f46435))
* **calendar:** keep backslashes and commas in .ics text exactly ([#62](https://github.com/starLORD03/usmp/issues/62)) ([d1771a9](https://github.com/starLORD03/usmp/commit/d1771a983f6d33ae52db497ad21db7aca69ee167))
* **testing:** correct end times in the synthetic test calendar ([#54](https://github.com/starLORD03/usmp/issues/54)) ([1e24aca](https://github.com/starLORD03/usmp/commit/1e24acae7fc2c4045317b8cdfab7c7d3933e2744))

## 0.1.0 (2026-11-22)


### Features

* add Python scaffold, CI pipeline, release and docs automation ([#10](https://github.com/starLORD03/usmp/issues/10)) ([199cefc](https://github.com/starLORD03/usmp/commit/199cefc0b8b0061af2045bdf470fd89a78b56f26))
* **calendar:** add CalDAV connector with keychain credentials ([#38](https://github.com/starLORD03/usmp/issues/38)) ([ab4f537](https://github.com/starLORD03/usmp/commit/ab4f537f6105b22814d9f1dc27e4cd1b9b7480d5))
* **calendar:** add calendar canonical model and domain validation ([#36](https://github.com/starLORD03/usmp/issues/36)) ([5573efa](https://github.com/starLORD03/usmp/commit/5573efa23e8b9cf742f2d860af2f76358deb5e07))
* **calendar:** add Google Calendar connector ([#39](https://github.com/starLORD03/usmp/issues/39)) ([b1763cf](https://github.com/starLORD03/usmp/commit/b1763cf115f21ca969221d7cde06e47c38fd370e))
* **calendar:** add iCalendar (.ics) file connector ([#37](https://github.com/starLORD03/usmp/issues/37)) ([f7c1219](https://github.com/starLORD03/usmp/commit/f7c1219e643279d03fa19510bef0c546f18e6670))
* **calendar:** keep meeting links, rooms and Google-only details ([#42](https://github.com/starLORD03/usmp/issues/42)) ([418110f](https://github.com/starLORD03/usmp/commit/418110fff9efd82030a7d101f709353fcd18c3b6))
* **calendar:** pass the 1,000-event release test on every path ([#44](https://github.com/starLORD03/usmp/issues/44)) ([7cc2501](https://github.com/starLORD03/usmp/commit/7cc25017a3f7ff639c823abb033004d736d74cda))
* **cli:** add --no-browser to usmp auth google and ignore client secrets ([#40](https://github.com/starLORD03/usmp/issues/40)) ([2224f3a](https://github.com/starLORD03/usmp/commit/2224f3af251190b65b2d52f47ee50ce1d0e985ff))
* **cli:** guide users through usmp migrate step by step ([#43](https://github.com/starLORD03/usmp/issues/43)) ([c2c3caa](https://github.com/starLORD03/usmp/commit/c2c3caa7d08cd4424816338993082b891d8758dd))
* **core:** add .usmp bundle reader, writer and bundle CLI ([#26](https://github.com/starLORD03/usmp/issues/26)) ([fc86fbc](https://github.com/starLORD03/usmp/commit/fc86fbcbf88389aeb5eca767b5e6055881399663))
* **core:** add migration engine, verifier, reports and usmp migrate ([#28](https://github.com/starLORD03/usmp/issues/28)) ([993d2e2](https://github.com/starLORD03/usmp/commit/993d2e229e73447a2a7415c2eb45f352023bde25))
* **core:** add protocol models, JSON Schemas and spec draft ([#25](https://github.com/starLORD03/usmp/issues/25)) ([c7847fd](https://github.com/starLORD03/usmp/commit/c7847fd5626c7ffe255f7e18129a5d7d0098508a))
* **sdk:** add connector conformance test kit and connector guide ([#29](https://github.com/starLORD03/usmp/issues/29)) ([190f888](https://github.com/starLORD03/usmp/commit/190f88853595d87b20d49d794dfc072026c75ade))
* **sdk:** add connector SDK, plugin registry and built-in connectors ([#27](https://github.com/starLORD03/usmp/issues/27)) ([257507e](https://github.com/starLORD03/usmp/commit/257507e1c5b7ce563bd3493ccb5389a804674f66))


### Bug Fixes

* **calendar:** copy Google default reminders and show the sign-in link at once ([#41](https://github.com/starLORD03/usmp/issues/41)) ([2c5b0e3](https://github.com/starLORD03/usmp/commit/2c5b0e33ecf607a18916fd8c3f7f487360305579))


### Documentation

* add v3 architecture overview, phased roadmap and ADRs 0001-0006 ([2695410](https://github.com/starLORD03/usmp/commit/269541095d9695d440db4d76b9a0d19910f33c7a))
* v3 architecture, phased roadmap and ADRs ([94ff51f](https://github.com/starLORD03/usmp/commit/94ff51fff5e258c3c394d27e63e12ccd4f876c16))
