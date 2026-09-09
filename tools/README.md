# tools/

Maintenance scripts. Neither of these is part of a research run.

## audit_domains.py

Finds the Chinese domains whose text the pipeline can actually read.

The local-domain channel was first configured by guessing — 天眼查, 企查查,
爱企查 and ccgp.gov.cn, the sites a web search keeps pointing at. Three turned
out to be login-walled and the fourth blocks automated fetching in robots.txt,
so that channel produced 78 records for Unitree and not one document.

This picks candidates the other way round. Baidu has already said which
domains it returns for these companies — the saved raw responses hold them —
so the only question is which of those serve their body text to a normal
request, and that is answerable by fetching one saved URL per domain. No
SerpApi quota is spent.

```bash
python tools/audit_domains.py 'research/*/*'
```

Point it at run directories; it reads their `raw_*responses.json`. Output is
ranked, with a READABLE section to copy into `local_sources.domains` and a
REJECTED section giving each host's reason (gated, robots-blocked, JS-rendered,
WAF challenge, or a body too thin to be anything but navigation).

First full run: 114 hosts, 53 readable, 23 thin, 16 error, 11 gated,
11 blocked.

Two cautions:

- The domain list reflects the companies already researched, so it is biased
  toward their sector. Re-run it after adding runs in a new sector.
- A host that refuses today may allow tomorrow and the reverse. The report is
  a snapshot, not a permanent verdict.
