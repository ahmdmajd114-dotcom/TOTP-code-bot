# Feature boundaries

Keep each independent feature in its own clearly named module. New debt
storage, validation, keyboards, owner flows and repayment rules belong in
`debt_manager.py`. Keep `bot.py` to configuration, handler registration and
small integration calls for the feature. Do not import `bot.py` from feature
modules or start external clients at module import time.

Financial transactions and exact dinar validation belong in
`financial_ledger.py` and `supabase_financial_ledger.sql`. Income, expenses,
wallets, customer balances and reports have their own `*_manager.py` modules.
`finance_integration.py` is the store adapter; `finance_projection.py` mirrors
committed operations to Sheets without incrementing balances. Never write
financial effects through a legacy Sheet path after ledger activation.

Schema installation does not activate the new ledger. Preserve the explicit
legacy -> review -> active transition and owner review of opening balances.
Do not silently choose legacy mode on connectivity or authorization failures.

Preserve existing customer/payment data and the shared financial accounting
path. Verify monetary changes with failure, retry and duplicate-operation
tests as well as successful cases. Never treat a failed debt read as an empty
debt list or declare a financial operation complete before its writes succeed.
