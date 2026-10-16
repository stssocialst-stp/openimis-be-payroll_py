# openIMIS Backend payroll reference module

## Payroll and Benefit Management Models and Their Fields

### PayrollStatus
- Represents the status of a payroll.
- Available statuses:
  - PENDING_APPROVAL
  - APPROVE_FOR_PAYMENT
  - REJECTED
  - RECONCILED

### BenefitConsumptionStatus
- Represents the status of benefit consumption.
- Available statuses:
  - ACCEPTED
  - CREATED
  - APPROVE_FOR_PAYMENT
  - REJECTED
  - DUPLICATE
  - RECONCILED

### PaymentPoint
- **name**: Name of the payment point.
- **location**: Foreign key to `Location`.
- **ppm**: Foreign key to `User`.

### Payroll
- **name**: Name of the payroll.
- **payment_plan**: Foreign key to `PaymentPlan`.
- **payment_cycle**: Foreign key to `PaymentCycle`.
- **payment_point**: Foreign key to `PaymentPoint`.
- **status**: Status of the payroll (uses `PayrollStatus` choices).
- **payment_method**: Method of payment.

### PayrollBill
- **payroll**: Foreign key to `Payroll`.
- **bill**: Foreign key to `Bill`.

### PaymentAdaptorHistory
- **payroll**: Foreign key to `Payroll`.
- **total_amount**: Total amount as a string.
- **bills_ids**: JSON field for bill IDs.

### BenefitConsumption
- **individual**: Foreign key to `Individual`.
- **photo**: Text field for photo.
- **code**: Code for benefit consumption.
- **date_due**: Due date.
- **receipt**: Receipt number.
- **amount**: Amount (decimal).
- **type**: Type of benefit.
- **status**: Status of benefit consumption (uses `BenefitConsumptionStatus` choices).

### BenefitAttachment
- **benefit**: Foreign key to `BenefitConsumption`.
- **bill**: Foreign key to `Bill`.

### PayrollBenefitConsumption
- **payroll**: Foreign key to `Payroll`.
- **benefit**: Foreign key to `BenefitConsumption`.

### CsvReconciliationUpload
- **payroll**: Foreign key to `Payroll`.
- **status**: Status of the reconciliation upload (uses `CsvReconciliationUpload.Status` choices).
- **error**: JSON field for errors.
- **file_name**: Name of the file.

### PayrollMutation
- **payroll**: Foreign key to `Payroll`.
- **mutation**: Foreign key to `MutationLog`.

## Digital Means of Payment Configuration

This section details the configuration settings required for integrating with a digital payment gateway. The settings are defined in the application's configuration file (`apps.py`).

### Configuration Parameters

- **gateway_base_url**: The base URL of the payment gateway's API.
  - Example: `"http://41.175.18.170:8070/api/mobile/v1/"`

- **endpoint_payment**: The endpoint for processing payments.
  - Example: `"mock/payment"`

- **endpoint_reconciliation**: The endpoint for reconciling payments.
  - Example: `"mock/reconciliation"`

- **payment_gateway_api_key**: The API key for authenticating with the payment gateway. It is retrieved from environment variables.

- **payment_gateway_basic_auth_username**: The username for basic authentication with the payment gateway. It is retrieved from environment variables.

- **payment_gateway_basic_auth_password**: The password for basic authentication with the payment gateway. It is retrieved from environment variables.

- **payment_gateway_timeout**: The timeout setting for requests to the payment gateway, in seconds.
  - Example: `5`

- **payment_gateway_auth_type**: The type of authentication used by the payment gateway. It can be either 'token' or 'basic'.
  - Example: `"basic"`

- **payment_gateway_class**: The class that handles interactions with the payment gateway.
  - Example: `"payroll.payment_gateway.MockedPaymentGatewayConnector"`

- **receipt_length**: The length of the receipt generated for transactions.
  - Example: `8`

### Example Configuration

```python
{
    "gateway_base_url": "http://41.175.18.170:8070/api/mobile/v1/",
    "endpoint_payment": "mock/payment",
    "endpoint_reconciliation": "mock/reconciliation",
    "payment_gateway_api_key": os.getenv('PAYMENT_GATEWAY_API_KEY'),
    "payment_gateway_basic_auth_username": os.getenv('PAYMENT_GATEWAY_BASIC_AUTH_USERNAME'),
    "payment_gateway_basic_auth_password": os.getenv('PAYMENT_GATEWAY_BASIC_AUTH_PASSWORD'),
    "payment_gateway_timeout": 5,
    "payment_gateway_auth_type": "basic",
    "payment_gateway_class": "payroll.payment_gateway.MockedPaymentGatewayConnector",
    "receipt_length": 8
}
```

### Integrating the Payment Gateway

To integrate the payment gateway, you need to define a class that extends the `PaymentGatewayConnector` and implements the necessary methods to handle payment and reconciliation requests.

### Example Implementation of Payment Gateway Integration

```python
from payroll.payment_gateway.payment_gateway_connector import PaymentGatewayConnector

class MockedPaymentGatewayConnector(PaymentGatewayConnector):
    def send_payment(self, invoice_id, amount, **kwargs):
        payload = {"invoiceId": str(invoice_id), "amount": str(amount)}
        response = self.send_request(self.config.endpoint_payment, payload)
        if response:
            response_text = response.text
            expected_message = f"{invoice_id} invoice of {amount} accepted to be paid"
            if response_text == expected_message:
                return True
        return False

    def reconcile(self, invoice_id, amount, **kwargs):
        payload = {"invoiceId": str(invoice_id), "amount": str(amount)}
        response = self.send_request(self.config.endpoint_reconciliation, payload)
        if response:
            return response.text == "true"
        return False
```

## Environment Variables

Make sure to set the following environment variables in your environment:

- `PAYMENT_GATEWAY_API_KEY`: Your API key for the payment gateway.
- `PAYMENT_GATEWAY_BASIC_AUTH_USERNAME`: Your username for basic authentication.
- `PAYMENT_GATEWAY_BASIC_AUTH_PASSWORD`: Your password for basic authentication.

You can use either basic authentication or token authentication. Set the following variable accordingly:

## Payment Flow for Online Payroll Payments

When the `payment_method` of a Payroll is set to `StrategyOnlinePayment`, the configuration described below is required for the payment and reconciliation process.

### Instructions for Making a Payment

1. **Populate/Generate Payroll**: Ensure the payroll has `payment_method='StrategyOnlinePayment'`.
2. **Accept or Reject Payroll**: Use the maker-checker logic to either accept or reject the payroll.
3. **Accepted Payroll**:
   - Navigate to `Legal and Finance (Payments) -> Accepted Payrolls`.
   - Click the `Make Payment` button. This triggers the payment flow defined in the configuration.
4. **Invoice Submission**:
   - If all invoices are sent successfully, go to the view reconciliation summary.
   - Invoices that were accepted will have their status changed from `Accepted` to `Approved for Payment`.
5. **Close Payroll**:
   - In the same view, click the `Accept and Close Payroll` button.
6. **Task View**:
   - Go to payroll details and click the `Tasks` view.
   - At the top of the searcher, you should see a `Reconciliation` task.
7. **Accept or Reject Task**:
   - Accept or reject the reconciliation task.
   - If accepted, the reconciliation flow is triggered. Payments are reconciled if the feedback from the payment gateway is successful without any issues. The status for the benefit will be `Reconciled`. The payroll will now be visible in `Payrolls -> Reconciled Payrolls`.
8. **Error Handling**:
   - Even if the payroll is reconciled, some benefits might not be paid due to errors.
   - The status will remain `Approved for Payment`.
   - Errors can be viewed by clicking the `Error` button.
   - Unpaid Payroll's invoices can be recreated during re-creation of payroll in reconciled payroll
9. **Recreate Unpaid Invoices**:
   - Unpaid payroll invoices can be recreated during the re-creation of payroll in the reconciled payroll section.
   - The unpaid invoices will be included in the new payroll.
   - Use the `Create Payroll from Unpaid Invoices` button available when you go to `Legal and Finance -> Reconciled Payrolls -> View Reconciled Payroll -> Create Payment from Failed Invoice`.

## Payment Flow for Offline Payroll Payments

When the `payment_method` of a Payroll is set to `StrategyOfflinePayment`, the configuration described below is required for the offline payment and reconciliation process.

### Instructions for Making an Offline Payment

1. **Create Payroll**:
   - Navigate to `Legal and Finance (Payments) -> Payrolls`.
   - Click the `+` (add payroll) button to create a new payroll.
2. **Choose Payment Method**:
   - Select `StrategyOfflinePayment` as the payment method.
3. **Accept or Reject Payroll**:
   - Use the maker-checker logic to either accept or reject the payroll.
   - Go to the `Tasks` tab on the payroll details page to perform this action.
4. **Accepted Payroll**:
   - If the payroll task is accepted, a new button `Upload Payment Data` becomes available.
   - Click `Download` to view the `invoices` attached to the payroll in CSV format.
5. **Prepare Reconciliation File**:
   - To reconcile a benefit/invoice, fill in the `Paid` column with `Yes` and generate a `Receipt` number.
   - If the payment is not made, do not add a receipt number and set the `Paid` column to `No`. Alternatively, remove the unpaid record from the file entirely.
6. **Upload Payment Data**:
   - Once the file is prepared, upload it by clicking `Upload Payment Data`.
   - After uploading, you should see the `payments` data reconciled based on the `Paid` status and the presence of a receipt number.
7. **Reconcile Payroll**:
   - Click `View Reconciliation Summary` under `Legal and Finance (Payments) -> Reconciled Payrolls`.
   - If you click `Approve and Close`, to confirm the reconciliation, go to `Tasks` either in the `All Tasks` view or on the `Payroll` page in the `Tasks` tab.
8. **Recreate Unpaid Invoices**:
   - Unpaid payroll invoices can be recreated during the re-creation of payroll in the reconciled payroll section.
   - The unpaid invoices will be included in the new payroll.
   - Use the `Create Payroll from Unpaid Invoices` button available when you go to `Legal and Finance -> Reconciled Payrolls -> View Reconciled Payroll -> Create Payment from Failed Invoice`.

# Sty
