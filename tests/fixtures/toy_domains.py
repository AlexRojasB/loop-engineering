"""
Generic (non-Inventory) fixtures reproducing the two Test Contract
failure patterns found in the Inventory benchmark run:

- WIDGET domain: fresh-instance contradictory setup (spec 005 class 1)
- LEDGER domain: quantitative-state contradiction (spec 005 class 2)

Domain names are intentionally unrelated to Inventory/SKU/reservation so
these fixtures exercise the *pattern*, not the specific benchmark.
"""

# ---------------------------------------------------------------------------
# WIDGET domain — fresh-instance contradictory setup
# ---------------------------------------------------------------------------

WIDGET_TASK = (
    "Add a WidgetRegistry.Register(string code, int quantity) method that "
    "registers a new widget under a unique code. Register must return false "
    "for a null/whitespace code, a negative quantity, or a code that is "
    "already registered. Register must return true and store the widget "
    "otherwise."
)

WIDGET_PRODUCTION = """
using System;
using System.Collections.Generic;
using System.Linq;

public class Widget
{
    public string Code { get; }
    public int Quantity { get; set; }

    public Widget(string code, int quantity)
    {
        Code = code;
        Quantity = quantity;
    }
}

public class WidgetRegistry
{
    private readonly List<Widget> _widgets = new();

    public bool Register(string code, int quantity)
    {
        if (string.IsNullOrWhiteSpace(code) || quantity < 0)
        {
            return false;
        }

        if (_widgets.Any(w => w.Code == code))
        {
            return false;
        }

        _widgets.Add(new Widget(code, quantity));
        return true;
    }

    public Widget FindByCode(string code)
    {
        return _widgets.FirstOrDefault(w => w.Code == code);
    }
}
"""

WIDGET_ORIGINAL_TEST_FILE = """
using System;
using Xunit;

public class WidgetRegistryTests
{
}
"""

# Reproduces the frozen, invalid contract observed at
# run-hybrid-inventory-full-001.txt:17782-17786 (Inventory-specific there;
# generalized here to the Widget domain). The registry is freshly
# constructed, so FindByCode can never return a non-null widget yet the
# snippet asserts Register fails for a brand-new, valid code.
WIDGET_BAD_SNIPPET_FRESH_INSTANCE_GUARD = """
[Fact]
public void Register_RejectsWhenCodeAlreadyExists()
{
    var registry = new WidgetRegistry();
    var widget = registry.FindByCode("W-1");

    if (widget == null)
    {
        Assert.False(registry.Register("W-1", 10));
        widget = registry.FindByCode("W-1");
    }

    Assert.NotNull(widget);
}
"""

WIDGET_GOOD_SNIPPET = """
[Fact]
public void Register_StoresNewWidgetWithGivenQuantity()
{
    var registry = new WidgetRegistry();

    Assert.True(registry.Register("W-1", 10));

    var widget = registry.FindByCode("W-1");
    Assert.Equal(10, widget.Quantity);
}
"""

# ---------------------------------------------------------------------------
# LEDGER domain — quantitative-state contradiction
# ---------------------------------------------------------------------------

LEDGER_TASK = (
    "Add Ledger.Withdraw(Guid accountId, int amount) to withdraw funds from "
    "an account. Withdraw must return false for a non-positive amount, an "
    "unknown account, or insufficient balance. On success it must reduce "
    "the account's Balance by amount and return true."
)

LEDGER_PRODUCTION = """
using System;
using System.Collections.Generic;
using System.Linq;

public class Account
{
    public Guid Id { get; }
    public int Balance { get; set; }

    public Account(int balance)
    {
        Id = Guid.NewGuid();
        Balance = balance;
    }
}

public class Ledger
{
    private readonly List<Account> _accounts = new();

    public Account Open(int initialBalance)
    {
        var account = new Account(initialBalance);
        _accounts.Add(account);
        return account;
    }

    public bool Withdraw(Guid accountId, int amount)
    {
        if (amount <= 0)
        {
            return false;
        }

        var account = _accounts.FirstOrDefault(a => a.Id == accountId);

        if (account is null)
        {
            return false;
        }

        if (account.Balance < amount)
        {
            return false;
        }

        account.Balance -= amount;

        return true;
    }
}
"""

LEDGER_ORIGINAL_TEST_FILE = """
using System;
using Xunit;

public class LedgerTests
{
}
"""

# Reproduces the frozen, invalid contract observed at
# run-hybrid-inventory-full-001.txt:17843-17862 (Inventory AvailableQuantity
# there; generalized here to Ledger.Balance). Withdraw is asserted to
# succeed, yet production unconditionally applies `balance -= amount` on
# success, so Balance cannot remain unchanged.
LEDGER_BAD_SNIPPET_QUANTITATIVE_CONTRADICTION = """
[Fact]
public void Withdraw_DoesNotChangeBalance_WhenSuccessful()
{
    var ledger = new Ledger();
    var account = ledger.Open(100);

    var originalBalance = account.Balance;
    Assert.True(ledger.Withdraw(account.Id, 20));

    Assert.Equal(originalBalance, account.Balance);
}
"""

LEDGER_GOOD_SNIPPET = """
[Fact]
public void Withdraw_ReducesBalance_WhenSuccessful()
{
    var ledger = new Ledger();
    var account = ledger.Open(100);

    var originalBalance = account.Balance;
    Assert.True(ledger.Withdraw(account.Id, 20));

    Assert.Equal(originalBalance - 20, account.Balance);
}
"""
