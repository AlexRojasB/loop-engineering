"""
Real-model evaluation of the contract-challenge adjudicator prompt.

NOT a unit test: this calls the configured local reviewer model and is
excluded from `unittest discover` by its filename. The deterministic
wiring is covered by tests/test_contract_challenge.py; what a specific
local model actually *decides* can only be measured by asking it.

Run:

    python tests/manual_eval_contract_challenge.py

Two cases, taken from the real Ledger Full #2 run:

1. IMPOSSIBLE - the frozen contract deposits into the id of an Account
   that CreateAccount never registered. Expect CONFIRM.
2. MERELY UNIMPLEMENTED - the same contract, but production already
   exposes a Find(name) lookup the test uses correctly, so the only
   thing missing is Deposit itself. Expect REJECT: test-first RED is not
   a contract defect.

The second case is the one that matters. A reviewer that confirms it
would let an agent discard a perfectly good contract by declining to
implement it.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.contract_challenge import (  # noqa: E402
    normalize_challenge,
    review_challenge,
)
from core.utils import load_json  # noqa: E402


PRODUCTION_PATH = "LedgerPipeline/Program.cs"

CONTRACT_PATH = "LedgerPipeline.Tests/UnitTest1.cs"

REAL_PRODUCTION = 'using System;\nusing System.Collections.Generic;\nusing System.Linq;\n\npublic class Account\n{\n    public Guid Id { get; }\n    public string Name { get; }\n    public decimal Balance { get; set; }\n\n    public Account(string name, decimal initialBalance)\n    {\n        Id = Guid.NewGuid();\n        Name = name;\n        Balance = initialBalance;\n    }\n}\n\npublic class LedgerService\n{\n    private readonly List<Account> _accounts = new();\n\n    public bool CreateAccount(string name, decimal initialBalance)\n    {\n        if (string.IsNullOrWhiteSpace(name) || initialBalance < 0)\n        {\n            return false;\n        }\n\n        if (_accounts.Any(\n            account =>\n                account.Name.Equals(\n                    name,\n                    StringComparison.OrdinalIgnoreCase\n                )\n        ))\n        {\n            return false;\n        }\n\n        _accounts.Add(\n            new Account(\n                name,\n                initialBalance\n            )\n        );\n\n        return true;\n    }\n\n    public Account? GetAccountByName(string name)\n    {\n        return _accounts.FirstOrDefault(\n            account =>\n                account.Name.Equals(\n                    name,\n                    StringComparison.OrdinalIgnoreCase\n                )\n        );\n    }\n\n    public IReadOnlyList<Account> GetAccounts()\n    {\n        return _accounts.ToList();\n    }\n}\n\npublic static class Program\n{\n    public static void Main()\n    {\n    }\n}\n'

REAL_FROZEN_CONTRACT = 'using Xunit;\n\npublic class LedgerServiceTests\n{\n    [Fact]\n    public void CreateAccount_WithValidData_ReturnsTrue()\n    {\n        var service = new LedgerService();\n\n        Assert.True(\n            service.CreateAccount(\n                "Checking",\n                100m\n            )\n        );\n    }\n\n    [Fact]\n    public void CreateAccount_WithNegativeBalance_ReturnsFalse()\n    {\n        var service = new LedgerService();\n\n        Assert.False(\n            service.CreateAccount(\n                "Checking",\n                -1m\n            )\n        );\n    }\n\n    [Fact]\n    public void GetAccountByName_IsCaseInsensitive()\n    {\n        var service = new LedgerService();\n\n        service.CreateAccount(\n            "Checking",\n            100m\n        );\n\n        var account =\n            service.GetAccountByName("checking");\n\n        Assert.NotNull(account);\n        Assert.Equal(\n            "Checking",\n            account!.Name\n        );\n    }\n\n    [Fact]\n    public void GetAccounts_DoesNotExposeInternalCollection()\n    {\n        var service = new LedgerService();\n\n        service.CreateAccount(\n            "Checking",\n            100m\n        );\n\n        var firstRead =\n            service.GetAccounts().ToList();\n\n        firstRead.Clear();\n\n        Assert.Single(\n            service.GetAccounts()\n        );\n    }\n\n    [Fact]\n    public void Deposit_WithNonExistentAccount_ReturnsFalse()\n    {\n        var service = new LedgerService();\n        var accountId = Guid.NewGuid();\n\n        Assert.False(service.Deposit(accountId, 100m));\n    }\n\n    [Fact]\n    public void Deposit_WithZeroAmount_ReturnsFalse()\n    {\n        var service = new LedgerService();\n        var account = new Account("Checking", 100m);\n        service.CreateAccount(account.Name, account.Balance);\n\n        Assert.False(service.Deposit(account.Id, 0m));\n    }\n\n    [Fact]\n    public void Deposit_WithNegativeAmount_ReturnsFalse()\n    {\n        var service = new LedgerService();\n        var account = new Account("Checking", 100m);\n        service.CreateAccount(account.Name, account.Balance);\n\n        Assert.False(service.Deposit(account.Id, -1m));\n    }\n\n    [Fact]\n    public void Deposit_WithValidAccountAndAmount_ReturnsTrue()\n    {\n        var service = new LedgerService();\n        var account = new Account("Checking", 100m);\n        service.CreateAccount(account.Name, account.Balance);\n\n        Assert.True(service.Deposit(account.Id, 50m));\n    }\n\n    [Fact]\n    public void Deposit_WithValidAccountAndAmount_IncreasesBalance()\n    {\n        var service = new LedgerService();\n        var account = new Account("Checking", 100m);\n        service.CreateAccount(account.Name, account.Balance);\n\n        service.Deposit(account.Id, 50m);\n\n        var updatedAccount = service.GetAccountByName(account.Name);\n        Assert.NotNull(updatedAccount);\n        Assert.Equal(150m, updatedAccount.Balance);\n    }\n}'

TASK = """
# Deposit Funds

## Requirements

1. Add a public method named `Deposit`.
2. The method receives an account id and an amount.
3. Return false when the account does not exist.
4. Return false when the amount is zero or negative.
5. On success, increase the account balance by the deposited amount.
6. Return true when the deposit succeeds.
7. Failed deposits must not modify account state.
8. Preserve all existing behavior.
"""

# Verbatim from the benchmark repository: Account and LedgerService live
# in the same file, so the authorized implementation target already shows
# the constructor that generates a fresh identifier.
IMPOSSIBLE_PRODUCTION = REAL_PRODUCTION

# Verbatim from run-ledger-full-002.txt: the contract that was frozen,
# approved by both reviewers, and could not be satisfied.
IMPOSSIBLE_CONTRACT = REAL_FROZEN_CONTRACT

# The same repository, but a contract whose setup is correct: the id
# comes from the service's own lookup. Nothing is wrong here except that
# Deposit does not exist yet.
SATISFIABLE_CONTRACT = """
public class LedgerServiceTests
{
    [Fact]
    public void Deposit_WithValidAccountAndAmount_ReturnsTrue()
    {
        var service = new LedgerService();
        service.CreateAccount("Checking", 100m);
        var account = service.GetAccountByName("Checking");

        Assert.True(service.Deposit(account!.Id, 50m));
    }
}
"""

SATISFIABLE_PRODUCTION = REAL_PRODUCTION

FAILING_TEST = "Deposit_WithValidAccountAndAmount_ReturnsTrue"

EVIDENCE = """
exit_code=1
  Failed LedgerServiceTests.Deposit_WithValidAccountAndAmount_ReturnsTrue
  Assert.True() Failure
  Expected: True
  Actual:   False
"""

CASES = [
    {
        "name": "impossible identity/provenance contradiction",
        "expected": "CONFIRM",
        "production": {
            PRODUCTION_PATH: IMPOSSIBLE_PRODUCTION
        },
        "contract": {
            CONTRACT_PATH: IMPOSSIBLE_CONTRACT
        },
        "args": {
            "kind": "object_identity",
            "summary":
                "The test deposits into an id the service was never "
                "given.",
            "failing_tests": [FAILING_TEST],
            "authoritative_requirement":
                "Return false when the account does not exist; return "
                "true when the deposit succeeds.",
            "production_path": PRODUCTION_PATH,
            "production_quote":
                "Id = Guid.NewGuid();",
            "contradiction":
                "CreateAccount constructs and registers its own "
                "Account instance with a fresh id, so account.Id from "
                "the directly constructed instance is never present in "
                "_accounts. Deposit(account.Id, 50m) must therefore "
                "return false per requirement 3, while the test "
                "asserts true.",
        },
    },
    {
        "name": "merely unimplemented feature",
        "expected": "REJECT",
        "production": {
            PRODUCTION_PATH: SATISFIABLE_PRODUCTION
        },
        "contract": {
            CONTRACT_PATH: SATISFIABLE_CONTRACT
        },
        "args": {
            "kind": "object_identity",
            "summary":
                "The test deposits into an id the service was never "
                "given.",
            "failing_tests": [FAILING_TEST],
            "authoritative_requirement":
                "Return true when the deposit succeeds.",
            "production_path": PRODUCTION_PATH,
            "production_quote":
                "Id = Guid.NewGuid();",
            "contradiction":
                "CreateAccount constructs its own Account instance, so "
                "the id used by the test cannot be found and Deposit "
                "can never return true.",
        },
    },
]


def main():
    config = load_json(
        REPO_ROOT / "config.json"
    )

    results = []

    for case in CASES:
        challenge, error = normalize_challenge(
            case["args"]
        )

        if error:
            raise SystemExit(
                f"fixture is malformed: {error}"
            )

        print()
        print("=" * 60)
        print(f"CASE: {case['name']}")
        print(f"EXPECTED: {case['expected']}")
        print("=" * 60)

        review = review_challenge(
            config,
            TASK,
            case["contract"],
            case["production"],
            challenge,
            EVIDENCE
        )

        actual = (
            "CONFIRM"
            if review["confirmed"]
            else "REJECT"
        )

        results.append(
            {
                "case": case["name"],
                "expected": case["expected"],
                "actual": actual,
                "reasons": review["reasons"],
                "verdicts": [
                    {
                        "reviewer": item["reviewer"],
                        "decision": item["decision"],
                        "status": item["status"]
                    }
                    for item in review["reviews"]
                ],
            }
        )

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(
        json.dumps(
            results,
            indent=2
        )
    )

    failures = [
        item
        for item in results
        if item["actual"] != item["expected"]
    ]

    print()

    if failures:
        print(
            f"{len(failures)}/{len(results)} case(s) did not match "
            "the expected adjudication."
        )
        return 1

    print(
        f"All {len(results)} case(s) adjudicated as expected."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
