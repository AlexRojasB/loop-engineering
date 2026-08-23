Add support for refunding paid orders.

Requirements:

1. Add Refunded to OrderStatus.
2. Add RefundOrder(Guid orderId) to OrderService.
3. RefundOrder returns false when the order does not exist.
4. Only Paid orders can transition to Refunded.
5. Pending, Cancelled, and already Refunded orders cannot be refunded.
6. Existing behavior must continue working.
7. Add appropriate automated tests for the new behavior.
8. Build and all tests must pass.
