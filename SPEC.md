Implement a C# order service.

Requirements:

1. Create an Order class with Guid Id, decimal Total, string CustomerEmail, and OrderStatus Status.
2. OrderStatus must contain Pending, Paid, and Cancelled.
3. Create an OrderService that stores orders in memory.
4. CreateOrder must reject null or whitespace customer emails.
5. CreateOrder must reject totals less than or equal to zero.
6. Every created order must receive a unique Guid Id and start with Pending status.
7. PayOrder must return false if the order does not exist.
8. PayOrder must only transition Pending orders to Paid. Cancelled or already Paid orders cannot be paid.
9. CancelOrder must return false if the order does not exist.
10. CancelOrder must only transition Pending orders to Cancelled. Paid orders cannot be cancelled.
11. GetOrdersByCustomer must compare email addresses case-insensitively.
12. GetOrdersByCustomer must not expose the internal mutable collection.
