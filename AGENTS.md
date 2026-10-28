# Feature boundaries

Keep each independent feature in its own clearly named module. New debt
storage, validation, keyboards, owner flows and repayment rules belong in
`debt_manager.py`. Keep `bot.py` to configuration, handler registration and
small integration calls for the feature. Do not import `bot.py` from feature
modules or start external clients at module import time.

Preserve existing customer/payment data and the shared financial accounting
path. Verify monetary changes with failure, retry and duplicate-operation
tests as well as successful cases. Never treat a failed debt read as an empty
debt list or declare a financial operation complete before its writes succeed.
