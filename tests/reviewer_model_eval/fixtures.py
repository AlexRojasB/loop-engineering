"""
Deterministic fixtures for the semantic-reviewer model comparison.

Every case supplies exactly what the production Test Contract phase hands
`semantic_test_review_prompt`:

    task                        the authoritative specification text
    implementation_files        {path: current production source}
    merged_test_content         the candidate contract after merge
    authorized_future           rendered deterministic-gate evidence

Nothing here is model-specific. Both models under evaluation receive the
byte-identical rendered prompt.
"""

PRODUCTION_PATH = "LedgerPipeline/Program.cs"

CONTRACT_PATH = "LedgerPipeline.Tests/UnitTest1.cs"


# ---------------------------------------------------------------------------
# Production variants
# ---------------------------------------------------------------------------

_HEADER = """using System;
using System.Collections.Generic;
using System.Linq;

public class Account
{
    public Guid Id { get; }
    public string Name { get; }
    public decimal Balance { get; set; }

    public Account(string name, decimal initialBalance)
    {
        Id = Guid.NewGuid();
        Name = name;
        Balance = initialBalance;
    }
}

public class Transaction
{
    public Guid Id { get; }
    public Guid FromAccountId { get; }
    public Guid ToAccountId { get; }
    public decimal Amount { get; }
    public DateTime CreatedAt { get; }

    public Transaction(
        Guid fromAccountId,
        Guid toAccountId,
        decimal amount
    )
    {
        Id = Guid.NewGuid();
        FromAccountId = fromAccountId;
        ToAccountId = toAccountId;
        Amount = amount;
        CreatedAt = DateTime.UtcNow;
    }
}

public class LedgerService
{
    private readonly List<Account> _accounts = new();
    private readonly List<Transaction> _transactions = new();

    public bool CreateAccount(string name, decimal initialBalance)
    {
        if (string.IsNullOrWhiteSpace(name) || initialBalance < 0)
        {
            return false;
        }

        if (_accounts.Any(
            account =>
                account.Name.Equals(
                    name,
                    StringComparison.OrdinalIgnoreCase
                )
        ))
        {
            return false;
        }

        _accounts.Add(
            new Account(
                name,
                initialBalance
            )
        );

        return true;
    }

    public Account? GetAccountByName(string name)
    {
        return _accounts.FirstOrDefault(
            account =>
                account.Name.Equals(
                    name,
                    StringComparison.OrdinalIgnoreCase
                )
        );
    }

    public Account? GetAccountById(Guid id)
    {
        return _accounts.FirstOrDefault(
            account => account.Id == id
        );
    }

    public IReadOnlyList<Account> GetAccounts()
    {
        return _accounts.ToList();
    }

    public IReadOnlyList<Transaction> GetTransactions()
    {
        return _transactions.ToList();
    }

    public bool Withdraw(Guid accountId, decimal amount)
    {
        if (amount <= 0)
        {
            return false;
        }

        var account = GetAccountById(accountId);

        if (account == null)
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
"""

_DEPOSIT = """
    public bool Deposit(Guid accountId, decimal amount)
    {
        if (amount <= 0)
        {
            return false;
        }

        var account = GetAccountById(accountId);

        if (account == null)
        {
            return false;
        }

        account.Balance += amount;

        return true;
    }
"""

_TRANSFER = """
    public bool Transfer(
        Guid fromAccountId,
        Guid toAccountId,
        decimal amount
    )
    {
        if (amount <= 0 || fromAccountId == toAccountId)
        {
            return false;
        }

        var source = GetAccountById(fromAccountId);
        var target = GetAccountById(toAccountId);

        if (source == null || target == null)
        {
            return false;
        }

        if (source.Balance < amount)
        {
            return false;
        }

        source.Balance -= amount;
        target.Balance += amount;

        _transactions.Add(
            new Transaction(
                fromAccountId,
                toAccountId,
                amount
            )
        );

        return true;
    }
"""

_CLOSE_ACCOUNT = """
    public bool CloseAccount(Guid accountId)
    {
        var account = GetAccountById(accountId);

        if (account == null)
        {
            return false;
        }

        if (account.Balance != 0m)
        {
            return false;
        }

        _accounts.Remove(account);

        return true;
    }
"""

_FOOTER = """}

public static class Program
{
    public static void Main()
    {
    }
}
"""


def _production(*extra):
    return _HEADER + "".join(extra) + _FOOTER


# Cases 1, 2, 5, 6, 9, 10: no Deposit, no CloseAccount.
PROD_BASE = _production(_TRANSFER)

# Cases 3, 4: Transfer exists with three parameters; Transaction has no
# Description property. Deposit and CloseAccount already shipped.
PROD_TRANSFER_ERA = _production(
    _DEPOSIT,
    _TRANSFER,
    _CLOSE_ACCOUNT
)

# Cases 7, 8: Withdraw and CloseAccount both already exist. The task adds
# a guard, it does not change the arithmetic.
PROD_QUANTITATIVE = _production(
    _DEPOSIT,
    _TRANSFER,
    _CLOSE_ACCOUNT
)


# ---------------------------------------------------------------------------
# Existing frozen tests carried into every merged contract
# ---------------------------------------------------------------------------

_EXISTING_TESTS = """using System;
using System.Linq;
using Xunit;

public class LedgerServiceTests
{
    [Fact]
    public void CreateAccount_WithValidData_ReturnsTrue()
    {
        var service = new LedgerService();

        Assert.True(
            service.CreateAccount(
                "Checking",
                100m
            )
        );
    }

    [Fact]
    public void CreateAccount_WithNegativeBalance_ReturnsFalse()
    {
        var service = new LedgerService();

        Assert.False(
            service.CreateAccount(
                "Checking",
                -1m
            )
        );
    }

    [Fact]
    public void GetAccountByName_IsCaseInsensitive()
    {
        var service = new LedgerService();

        service.CreateAccount(
            "Checking",
            100m
        );

        var account = service.GetAccountByName("checking");

        Assert.NotNull(account);
        Assert.Equal(
            "Checking",
            account!.Name
        );
    }
"""


def _contract(*methods):
    return _EXISTING_TESTS + "".join(methods) + "}\n"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

TASK_CLOSE_ACCOUNT = """
# Close Account

## Requirements

1. Add a public method named `CloseAccount` to `LedgerService`.
2. `CloseAccount` receives the account id.
3. Return false when the account does not exist.
4. Return false when the account balance is not zero.
5. On success, remove the account from the ledger and return true.
6. Preserve all existing behavior.
""".strip()

TASK_TRANSFER_DESCRIPTION = """
# Transfer Description

## Requirements

1. Extend successful transfers so the caller may supply an optional
   description for the transfer.
2. The description is optional. Existing callers that do not supply one
   must keep working unchanged.
3. A supplied description is normalized before it is stored: surrounding
   whitespace is trimmed.
4. A description that is null, empty, or whitespace only is stored as an
   empty string.
5. The recorded transaction must expose the normalized description.
6. Failed transfers must not record a transaction.
7. Preserve all existing behavior.
""".strip()

TASK_DEPOSIT = """
# Deposit Funds

## Requirements

1. Add a public method named `Deposit` to `LedgerService`.
2. The method receives an account id and an amount.
3. Return false when the account does not exist.
4. Return false when the amount is zero or negative.
5. On success, increase the account balance by the deposited amount.
6. Return true when the deposit succeeds.
7. Failed deposits must not modify account state.
8. Preserve all existing behavior.
""".strip()

TASK_WITHDRAW_CLOSED = """
# Reject Withdrawals From Closed Accounts

## Requirements

1. Closing an account must record that the account is closed rather than
   silently dropping it, so that later operations can detect it.
2. `Withdraw` must return false when the target account has been closed.
3. All other existing `Withdraw` behavior is unchanged: a successful
   withdrawal still reduces the account balance by the withdrawn amount.
4. Preserve all existing behavior.
""".strip()


# ---------------------------------------------------------------------------
# Authorized-future evidence, exactly as core.authorized_future renders it
# ---------------------------------------------------------------------------

AF_CLOSE_ACCOUNT = [
    {
        "symbol": "LedgerService.CloseAccount",
        "reason":
            "Requirement 1 of the current authoritative task adds a "
            "public method named `CloseAccount` to `LedgerService`.",
        "code": "CS1061",
        "message":
            "'LedgerService' does not contain a definition for "
            "'CloseAccount'"
    }
]

AF_TRANSFER_DESCRIPTION = [
    {
        "symbol": "LedgerService.Transfer(Guid, Guid, decimal, string)",
        "reason":
            "Requirement 1 of the current authoritative task extends "
            "successful transfers with an optional description.",
        "code": "CS1501",
        "message":
            "No overload for method 'Transfer' takes 4 arguments"
    },
    {
        "symbol": "Transaction.Description",
        "reason":
            "Requirement 5 of the current authoritative task requires "
            "the recorded transaction to expose the normalized "
            "description.",
        "code": "CS1061",
        "message":
            "'Transaction' does not contain a definition for "
            "'Description'"
    }
]

AF_DEPOSIT = [
    {
        "symbol": "LedgerService.Deposit",
        "reason":
            "Requirement 1 of the current authoritative task adds a "
            "public method named `Deposit` to `LedgerService`.",
        "code": "CS1061",
        "message":
            "'LedgerService' does not contain a definition for "
            "'Deposit'"
    }
]

AF_WITHDRAW_CLOSED = [
    {
        "symbol": "Account.IsClosed",
        "reason":
            "Requirement 1 of the current authoritative task requires "
            "closing an account to record that it is closed.",
        "code": "CS1061",
        "message":
            "'Account' does not contain a definition for 'IsClosed'"
    }
]


# ---------------------------------------------------------------------------
# Diagnostics the deterministic gate would see and REFUSE to authorize
# ---------------------------------------------------------------------------
#
# `authorized_future` above models the diagnostics the contract
# compilation gate classifies as expected_red -- symbols the current spec
# asked for. These model the opposite: symbols the compiler reports
# missing that the spec never requested, which the gate classifies as
# INVALID and rejects before any reviewer runs.
#
# They exist so the offline evaluation can exercise the real
# `classify_contract_diagnostics` path without a dotnet toolchain, and so
# it can measure how many semantic reviewer calls the deterministic gate
# makes unnecessary. Only case 4 has any: every other contract in the
# suite references either production API or task-authorized future API.

UAF_NONE = []

UAF_INVENTED_API = [
    {
        "symbol": "TransferRequest",
        "code": "CS0246",
        "message":
            "The type or namespace name 'TransferRequest' could not be "
            "found (are you missing a using directive or an assembly "
            "reference?)"
    },
    {
        "symbol": "TransferMetadata",
        "code": "CS0246",
        "message":
            "The type or namespace name 'TransferMetadata' could not be "
            "found (are you missing a using directive or an assembly "
            "reference?)"
    },
    {
        "symbol": "TransferChannel",
        "code": "CS0246",
        "message":
            "The type or namespace name 'TransferChannel' could not be "
            "found (are you missing a using directive or an assembly "
            "reference?)"
    }
]


# ---------------------------------------------------------------------------
# Candidate contracts
# ---------------------------------------------------------------------------

# CASE 1 -- the Ledger Spec 007 false-premise trap. The account is created
# with 100m, which is ALREADY non-zero. Nothing further is needed to reach
# the non-zero precondition. Identity comes from the service's own lookup.
CONTRACT_NONZERO_VALID = _contract("""
    [Fact]
    public void CloseAccount_WithNonZeroBalance_ReturnsFalse()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        Assert.False(
            service.CloseAccount(account!.Id)
        );
    }

    [Fact]
    public void CloseAccount_WithUnknownAccount_ReturnsFalse()
    {
        var service = new LedgerService();

        Assert.False(
            service.CloseAccount(Guid.NewGuid())
        );
    }

    [Fact]
    public void CloseAccount_WithZeroBalance_ReturnsTrue()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Savings",
            0m
        );
        var account = service.GetAccountByName("Savings");

        Assert.True(
            service.CloseAccount(account!.Id)
        );
    }
""")

# CASE 2 -- genuinely wrong. The test claims to exercise the non-zero
# rejection path but arranges a zero balance, where requirement 5 says
# CloseAccount must SUCCEED. Assert.False contradicts the task.
CONTRACT_NONZERO_INVALID = _contract("""
    [Fact]
    public void CloseAccount_WithNonZeroBalance_ReturnsFalse()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            0m
        );
        var account = service.GetAccountByName("Checking");

        Assert.False(
            service.CloseAccount(account!.Id)
        );
    }

    [Fact]
    public void CloseAccount_WithUnknownAccount_ReturnsFalse()
    {
        var service = new LedgerService();

        Assert.False(
            service.CloseAccount(Guid.NewGuid())
        );
    }
""")

# CASE 3 -- uses ONLY the two symbols the deterministic gate authorized:
# the four-argument Transfer overload and Transaction.Description.
CONTRACT_AUTHORIZED_FUTURE = _contract("""
    [Fact]
    public void Transfer_WithDescription_StoresTrimmedDescription()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        service.CreateAccount(
            "Savings",
            0m
        );
        var source = service.GetAccountByName("Checking");
        var target = service.GetAccountByName("Savings");

        Assert.True(
            service.Transfer(
                source!.Id,
                target!.Id,
                25m,
                "  rent payment  "
            )
        );

        var transaction = service.GetTransactions().Single();

        Assert.Equal(
            "rent payment",
            transaction.Description
        );
    }

    [Fact]
    public void Transfer_WithWhitespaceDescription_StoresEmptyString()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        service.CreateAccount(
            "Savings",
            0m
        );
        var source = service.GetAccountByName("Checking");
        var target = service.GetAccountByName("Savings");

        Assert.True(
            service.Transfer(
                source!.Id,
                target!.Id,
                25m,
                "   "
            )
        );

        Assert.Equal(
            string.Empty,
            service.GetTransactions().Single().Description
        );
    }

    [Fact]
    public void Transfer_WithoutDescription_StillSucceeds()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        service.CreateAccount(
            "Savings",
            0m
        );
        var source = service.GetAccountByName("Checking");
        var target = service.GetAccountByName("Savings");

        Assert.True(
            service.Transfer(
                source!.Id,
                target!.Id,
                25m
            )
        );
    }
""")

# CASE 4 -- invents a `TransferRequest` type and a request-object Transfer
# overload. The task asks for an optional description parameter, never for
# a new request type, and the gate authorized no such symbol.
CONTRACT_INVENTED_API = _contract("""
    [Fact]
    public void Transfer_WithTransferRequest_StoresTrimmedDescription()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        service.CreateAccount(
            "Savings",
            0m
        );
        var source = service.GetAccountByName("Checking");
        var target = service.GetAccountByName("Savings");

        var request = new TransferRequest
        {
            FromAccountId = source!.Id,
            ToAccountId = target!.Id,
            Amount = 25m,
            Description = "  rent payment  ",
            Metadata = new TransferMetadata
            {
                Channel = TransferChannel.Online
            }
        };

        var receipt = service.Transfer(request);

        Assert.True(receipt.Succeeded);
        Assert.Equal(
            "rent payment",
            receipt.Transaction.Description
        );
    }

    [Fact]
    public void Transfer_WithoutDescription_StillSucceeds()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        service.CreateAccount(
            "Savings",
            0m
        );
        var source = service.GetAccountByName("Checking");
        var target = service.GetAccountByName("Savings");

        Assert.True(
            service.Transfer(
                source!.Id,
                target!.Id,
                25m
            )
        );
    }
""")

# CASE 5 -- the identity/provenance bug that broke Deposit. The Account is
# constructed directly; CreateAccount registers a DIFFERENT instance with a
# different generated Id, so account.Id is unreachable by any Deposit
# implementation, and account.Balance is not the instance the service owns.
CONTRACT_IDENTITY_BUG = _contract("""
    [Fact]
    public void Deposit_WithValidAccountAndAmount_ReturnsTrue()
    {
        var service = new LedgerService();
        var account = new Account(
            "Checking",
            100m
        );
        service.CreateAccount(
            account.Name,
            account.Balance
        );

        Assert.True(
            service.Deposit(
                account.Id,
                50m
            )
        );
    }

    [Fact]
    public void Deposit_WithValidAccountAndAmount_IncreasesBalance()
    {
        var service = new LedgerService();
        var account = new Account(
            "Checking",
            100m
        );
        service.CreateAccount(
            account.Name,
            account.Balance
        );

        service.Deposit(
            account.Id,
            50m
        );

        Assert.Equal(
            150m,
            account.Balance
        );
    }

    [Fact]
    public void Deposit_WithZeroAmount_ReturnsFalse()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        Assert.False(
            service.Deposit(
                account!.Id,
                0m
            )
        );
    }
""")

# CASE 6 -- same domain, correct provenance: the id and the asserted
# instance both come from the service's own lookup.
#
# FIXED 2026-09-02, after the 16K context evaluation
# (results/context-20260902-112156). This contract used to carry only the
# first three tests, which left requirement 3 (unknown account returns
# false), requirement 7 (a failed deposit must not modify state) and the
# negative half of requirement 4 with no test at all -- while case 9,
# whose whole point is the SAME uncovered requirement 3, was a strict
# SUPERSET of it and expected REJECT.
#
# The pair was therefore unsatisfiable: any reviewer consistent about
# requirement coverage had to get exactly one of 6 and 9 wrong, and at
# 16K qwen3.5:9b rejected case 6 citing requirements 3 and 7, which was
# correct about the contract and scored as a false reject. The benchmark
# was punishing the model for being right.
#
# Requirements 3, 4 and 7 are now covered, so this case tests only what
# it claims to test -- identity provenance -- and cases 6 and 9 differ by
# exactly one test: Deposit_WithNonExistentAccount_ReturnsFalse.
CONTRACT_IDENTITY_VALID = _contract("""
    [Fact]
    public void Deposit_WithValidAccountAndAmount_ReturnsTrue()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        Assert.True(
            service.Deposit(
                account!.Id,
                50m
            )
        );
    }

    [Fact]
    public void Deposit_WithValidAccountAndAmount_IncreasesBalance()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        service.Deposit(
            account!.Id,
            50m
        );

        var updated = service.GetAccountByName("Checking");

        Assert.Equal(
            150m,
            updated!.Balance
        );
    }

    [Fact]
    public void Deposit_WithZeroAmount_ReturnsFalse()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        Assert.False(
            service.Deposit(
                account!.Id,
                0m
            )
        );
    }

    [Fact]
    public void Deposit_WithNegativeAmount_ReturnsFalse()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        Assert.False(
            service.Deposit(
                account!.Id,
                -1m
            )
        );
    }

    [Fact]
    public void Deposit_WithNonExistentAccount_ReturnsFalse()
    {
        var service = new LedgerService();

        Assert.False(
            service.Deposit(
                Guid.NewGuid(),
                50m
            )
        );
    }

    [Fact]
    public void Deposit_WithZeroAmount_DoesNotModifyBalance()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        service.Deposit(
            account!.Id,
            0m
        );

        Assert.Equal(
            100m,
            service.GetAccountByName("Checking")!.Balance
        );
    }
""")

# CASE 7 -- quantitative contradiction. Production Withdraw applies
# `account.Balance -= amount` unconditionally when it returns true, and the
# task changes only the closed-account guard. Asserting the balance is
# unchanged after an asserted-successful Withdraw contradicts that.
CONTRACT_QUANTITATIVE_INVALID = _contract("""
    [Fact]
    public void Withdraw_FromClosedAccount_ReturnsFalse()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            0m
        );
        var account = service.GetAccountByName("Checking");
        service.CloseAccount(account!.Id);

        Assert.False(
            service.Withdraw(
                account.Id,
                10m
            )
        );
    }

    [Fact]
    public void Withdraw_FromOpenAccount_SucceedsAndLeavesBalance()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");
        var originalBalance = account!.Balance;

        Assert.True(
            service.Withdraw(
                account.Id,
                20m
            )
        );

        Assert.Equal(
            originalBalance,
            service.GetAccountByName("Checking")!.Balance
        );
    }
""")

# CASE 8 -- same production behavior, correct numeric transition.
CONTRACT_QUANTITATIVE_VALID = _contract("""
    [Fact]
    public void Withdraw_FromClosedAccount_ReturnsFalse()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            0m
        );
        var account = service.GetAccountByName("Checking");
        service.CloseAccount(account!.Id);

        Assert.False(
            service.Withdraw(
                account.Id,
                10m
            )
        );
    }

    [Fact]
    public void Withdraw_FromOpenAccount_ReducesBalanceByAmount()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");
        var originalBalance = account!.Balance;

        Assert.True(
            service.Withdraw(
                account.Id,
                20m
            )
        );

        Assert.Equal(
            originalBalance - 20m,
            service.GetAccountByName("Checking")!.Balance
        );
    }
""")

# CASE 9 -- every test present is individually valid, but requirement 3
# ("return false when the account does not exist") has no test at all.
CONTRACT_MISSING_SCENARIO = _contract("""
    [Fact]
    public void Deposit_WithValidAccountAndAmount_ReturnsTrue()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        Assert.True(
            service.Deposit(
                account!.Id,
                50m
            )
        );
    }

    [Fact]
    public void Deposit_WithValidAccountAndAmount_IncreasesBalance()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        service.Deposit(
            account!.Id,
            50m
        );

        Assert.Equal(
            150m,
            service.GetAccountByName("Checking")!.Balance
        );
    }

    [Fact]
    public void Deposit_WithZeroAmount_ReturnsFalse()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        Assert.False(
            service.Deposit(
                account!.Id,
                0m
            )
        );
    }

    [Fact]
    public void Deposit_WithNegativeAmount_ReturnsFalse()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        Assert.False(
            service.Deposit(
                account!.Id,
                -1m
            )
        );
    }

    [Fact]
    public void Deposit_WithZeroAmount_DoesNotModifyBalance()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        service.Deposit(
            account!.Id,
            0m
        );

        Assert.Equal(
            100m,
            service.GetAccountByName("Checking")!.Balance
        );
    }
""")

# CASE 10 -- the same task, fully covered, no semantic contradiction.
CONTRACT_COMPLETE_GOOD = _contract("""
    [Fact]
    public void Deposit_WithUnknownAccount_ReturnsFalse()
    {
        var service = new LedgerService();

        Assert.False(
            service.Deposit(
                Guid.NewGuid(),
                50m
            )
        );
    }

    [Fact]
    public void Deposit_WithValidAccountAndAmount_ReturnsTrue()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        Assert.True(
            service.Deposit(
                account!.Id,
                50m
            )
        );
    }

    [Fact]
    public void Deposit_WithValidAccountAndAmount_IncreasesBalance()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        service.Deposit(
            account!.Id,
            50m
        );

        Assert.Equal(
            150m,
            service.GetAccountByName("Checking")!.Balance
        );
    }

    [Fact]
    public void Deposit_WithZeroAmount_ReturnsFalse()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        Assert.False(
            service.Deposit(
                account!.Id,
                0m
            )
        );
    }

    [Fact]
    public void Deposit_WithNegativeAmount_ReturnsFalse()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        Assert.False(
            service.Deposit(
                account!.Id,
                -1m
            )
        );
    }

    [Fact]
    public void Deposit_WithNegativeAmount_DoesNotModifyBalance()
    {
        var service = new LedgerService();
        service.CreateAccount(
            "Checking",
            100m
        );
        var account = service.GetAccountByName("Checking");

        service.Deposit(
            account!.Id,
            -1m
        );

        Assert.Equal(
            100m,
            service.GetAccountByName("Checking")!.Balance
        );
    }
""")


CASES = [
    {
        "id": 1,
        "name": "non-zero balance false-premise trap",
        "group": "semantic_trap",
        "expected": "APPROVE",
        "task": TASK_CLOSE_ACCOUNT,
        "production": PROD_BASE,
        "contract": CONTRACT_NONZERO_VALID,
        "authorized_future": AF_CLOSE_ACCOUNT,
        "unauthorized_future": UAF_NONE,
        "note":
            "The account is created with 100m. 100m is already non-zero; "
            "no Deposit or extra balance mutation is needed. Rejecting "
            "for 'the balance is not made non-zero' is the Ledger Spec "
            "007 false premise."
    },
    {
        "id": 2,
        "name": "actually invalid close-account setup",
        "group": "legitimate_rejection",
        "expected": "REJECT",
        "task": TASK_CLOSE_ACCOUNT,
        "production": PROD_BASE,
        "contract": CONTRACT_NONZERO_INVALID,
        "authorized_future": AF_CLOSE_ACCOUNT,
        "unauthorized_future": UAF_NONE,
        "note":
            "Named for the non-zero rejection path but arranges a zero "
            "balance, where requirement 5 says CloseAccount must succeed."
    },
    {
        "id": 3,
        "name": "authorized future API",
        "group": "semantic_trap",
        "expected": "APPROVE",
        "task": TASK_TRANSFER_DESCRIPTION,
        "production": PROD_TRANSFER_ERA,
        "contract": CONTRACT_AUTHORIZED_FUTURE,
        "authorized_future": AF_TRANSFER_DESCRIPTION,
        "unauthorized_future": UAF_NONE,
        "note":
            "Uses only gate-authorized symbols. Mere absence from current "
            "production must not be a rejection reason."
    },
    {
        "id": 4,
        "name": "invented API",
        "group": "legitimate_rejection",
        "expected": "REJECT",
        "task": TASK_TRANSFER_DESCRIPTION,
        "production": PROD_TRANSFER_ERA,
        "contract": CONTRACT_INVENTED_API,
        "authorized_future": AF_TRANSFER_DESCRIPTION,
        "unauthorized_future": UAF_INVENTED_API,
        "note":
            "TransferRequest / TransferMetadata / TransferChannel / a "
            "receipt return type are not authorized by the task or the "
            "gate."
    },
    {
        "id": 5,
        "name": "identity / provenance bug",
        "group": "semantic_trap",
        "expected": "REJECT",
        "task": TASK_DEPOSIT,
        "production": PROD_BASE,
        "contract": CONTRACT_IDENTITY_BUG,
        "authorized_future": AF_DEPOSIT,
        "unauthorized_future": UAF_NONE,
        "note":
            "CreateAccount registers its own Account with a fresh "
            "Guid.NewGuid(), so the directly constructed account.Id is "
            "unreachable and account.Balance is not the owned instance."
    },
    {
        "id": 6,
        "name": "valid registered identity",
        "group": "clean_approval",
        "expected": "APPROVE",
        "task": TASK_DEPOSIT,
        "production": PROD_BASE,
        "contract": CONTRACT_IDENTITY_VALID,
        "authorized_future": AF_DEPOSIT,
        "unauthorized_future": UAF_NONE,
        "note":
            "Id and asserted instance both come from the service's own "
            "lookup."
    },
    {
        "id": 7,
        "name": "quantitative invariant contradiction",
        "group": "semantic_trap",
        "expected": "REJECT",
        "task": TASK_WITHDRAW_CLOSED,
        "production": PROD_QUANTITATIVE,
        "contract": CONTRACT_QUANTITATIVE_INVALID,
        "authorized_future": AF_WITHDRAW_CLOSED,
        "unauthorized_future": UAF_NONE,
        "note":
            "Withdraw applies `account.Balance -= amount` on success; "
            "the test asserts success AND an unchanged balance."
    },
    {
        "id": 8,
        "name": "valid quantitative contract",
        "group": "clean_approval",
        "expected": "APPROVE",
        "task": TASK_WITHDRAW_CLOSED,
        "production": PROD_QUANTITATIVE,
        "contract": CONTRACT_QUANTITATIVE_VALID,
        "authorized_future": AF_WITHDRAW_CLOSED,
        "unauthorized_future": UAF_NONE,
        "note":
            "Asserts the exact numeric transition originalBalance - 20m."
    },
    {
        "id": 9,
        "name": "missing required scenario",
        "group": "legitimate_rejection",
        "expected": "REJECT",
        "task": TASK_DEPOSIT,
        "production": PROD_BASE,
        "contract": CONTRACT_MISSING_SCENARIO,
        "authorized_future": AF_DEPOSIT,
        "unauthorized_future": UAF_NONE,
        "note":
            "Requirement 3 (unknown account returns false) has no test. "
            "NOTE: in production this is the STRUCTURAL reviewer's "
            "concern ('missing required scenario' is listed in "
            "prompts/test-reviewer.md); prompts/test-semantic-reviewer.md "
            "explicitly scopes itself away from coverage. Scored as "
            "requested, and also reported separately."
    },
    {
        "id": 10,
        "name": "complete good contract",
        "group": "clean_approval",
        "expected": "APPROVE",
        "task": TASK_DEPOSIT,
        "production": PROD_BASE,
        "contract": CONTRACT_COMPLETE_GOOD,
        "authorized_future": AF_DEPOSIT,
        "unauthorized_future": UAF_NONE,
        "note":
            "All six requirements covered, correct provenance, correct "
            "numeric transitions."
    },
]
