<div align="center">

# TelegramLens

**Lets Claude and Codex search the Korean stock chatter on Telegram before they answer**

[leetkey.kr/en](https://leetkey.kr/en/) | [한국어](https://github.com/Johnhyeon/telegramlens-mcp/blob/main/README.md) | [Patch notes](https://github.com/Johnhyeon/telegramlens-mcp/blob/main/PATCHNOTES.md)

</div>

---

In Korea, stock talk often reaches Telegram channels before it reaches the news. But nobody can read dozens of channels, and an AI cannot see Telegram at all.

TelegramLens collects posts from **the channels your own Telegram account has joined**, keeps them on your PC organized by stock, and lets the AI look them up. **It notes when each story first appeared, and it never invents a reason the posts don't give.**

TelegramLens is part of the [LeetKit](https://leetkey.kr/en/) FULL Package, used together with StockLens (prices and flows) and DartLens (Korean DART filings).

**Ask in English, get answers in English.** Posts are in Korean; the AI translates as it answers.

## A real answer

> Why did HanAll Biopharma fall today? And since when has it been talked about?

**2026-09-28 Telegram channels, in time order**

| Time | Posted in channels |
|---|---|
| 07:20 | Kiwoom Securities report summary: partner halts development in cutaneous lupus (CLE) |
| 07:53 | IBK Securities report: price target cut, limited impact on pipeline value |
| 08:01 | DAOL Investment & Securities report: price target cut |
| 08:46 | Roundup of the day's 3 reports |
| 15:45 | Market close wrap: HanAll Biopharma −6.87%, CLE development halted |

The reason given in the posts: partner Immunovant's phase 2 CLE trial missed its primary endpoint and development was stopped. After three days with no mentions, it first appeared at 07:20, 1 hour 40 minutes before the open, and spread to 6 posts across 4 channels before 9:00. (Translated from the Korean output.)

## What you can ask

```
Which stocks are most talked about on Telegram lately?
Any stocks whose mentions jumped compared with yesterday?
Pick out today's posts and links worth reading
```

```
Summarize posts about EcoPro and the articles attached to them
Find posts that mention solid-state batteries
```

## What it covers

- **Most talked about:** stocks mentioned most in a period, and stocks whose mentions suddenly jumped
- **How it spread:** first mention time and the spread over time for each stock
- **Search:** posts on industries and themes even without a stock name, including the articles and blogs attached to them
- **Link contents:** titles and excerpts of linked articles; DART links continue into the original filing through DartLens
- **Daily briefing:** the most-discussed stocks and what's worth reading, in one go

23 tools in all.

## Principles

- **No invented reasons.** Evidence is the channel posts and their links.
- **Timestamps.** When a story first appeared and when it spread.
- **Forwards vs. independent posts.** It tells a single post copied into many channels apart from many channels saying it on their own.
- **Stock names checked against the exchange list.** About 2,700 listed names, so look-alike words are not mistaken for stocks.

## Stays on your PC

Posts come only from channels your Telegram account has joined and are stored only on your PC. Nothing is sent to a server. Newly joined channels are picked up automatically. Collection runs while your PC is on.

## Where it runs

| | |
|---|---|
| AI apps | Claude Desktop, Codex (ChatGPT account), Claude Code |
| OS | Windows, macOS |
| Not supported | Claude.ai on the web (it cannot connect to your PC) |

The AI app's own subscription is separate from LeetKit.

## Setup and pricing

- **Setup:** install with a button in LeetKit Manager and paste your license key. No commands.
- **Telegram login:** once, with your phone number, from the TelegramLens card in the Manager.
- **Trial:** 14 days free with all three Lenses, email only, no card.
- **Pricing:** one-time payment, no subscription. TelegramLens is not sold alone; it comes in the LeetKit FULL Package. Checkout is Korean; overseas cards may not work, so email us first.

Trial and prices: **[leetkey.kr/en](https://leetkey.kr/en/)**

## Not investment advice

TelegramLens is a data tool that organizes Telegram channel posts for your AI to analyze. It is not an investment advisory, discretionary management or stock recommendation service. It does not recommend buying or selling any security and has no order execution. Channel posts may be untrue, and AI answers are for reference only. Investment decisions and their outcomes are your own responsibility.

## License

Proprietary software. The source code is not public, and a valid license key is required. See [LICENSE](https://github.com/Johnhyeon/telegramlens-mcp/blob/main/LICENSE). This repository holds the overview and patch notes only.

Contact: support@leetkey.kr · Made by Leetkey Lab (리트키랩)
