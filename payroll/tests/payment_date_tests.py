import datetime
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from payroll.models import PayrollStatus
from payroll.payment_gateway.bistp_gateway_connector import BistpGatewayConnector
from payroll.strategies.strategy_bistp_payment import StrategyBistpPayment
from payroll.views import _payroll_id_from_transaction_id
from payroll.validation import validate_payment_date, validate_payment_date_update, BISTP_PAYMENT_METHOD

TODAY = datetime.date.today()
TOMORROW = TODAY + datetime.timedelta(days=1)
YESTERDAY = TODAY - datetime.timedelta(days=1)


class PaymentDateValidationTest(SimpleTestCase):

    def test_bistp_requires_payment_date(self):
        errors = validate_payment_date({'payment_method': BISTP_PAYMENT_METHOD})
        self.assertEqual(len(errors), 1)

    def test_other_method_does_not_require_payment_date(self):
        self.assertEqual(validate_payment_date({'payment_method': 'StrategyOnlinePayment'}), [])

    def test_payment_date_in_past_is_rejected(self):
        errors = validate_payment_date({'payment_method': BISTP_PAYMENT_METHOD, 'payment_date': YESTERDAY})
        self.assertEqual(len(errors), 1)

    def test_payment_date_today_or_future_is_accepted(self):
        for value in (TODAY, TOMORROW):
            self.assertEqual(validate_payment_date({'payment_method': BISTP_PAYMENT_METHOD, 'payment_date': value}), [])

    @mock.patch('payroll.validation.BenefitConsumption.objects.filter')
    def test_update_allowed_before_sending(self, filter_mock):
        filter_mock.return_value.exists.return_value = False
        payroll = SimpleNamespace(status=PayrollStatus.APPROVE_FOR_PAYMENT)
        self.assertEqual(validate_payment_date_update(payroll, {'payment_date': TOMORROW}), [])

    @mock.patch('payroll.validation.BenefitConsumption.objects.filter')
    def test_update_locked_after_sending(self, filter_mock):
        filter_mock.return_value.exists.return_value = True
        payroll = SimpleNamespace(status=PayrollStatus.APPROVE_FOR_PAYMENT)
        self.assertEqual(len(validate_payment_date_update(payroll, {'payment_date': TOMORROW})), 1)

    def test_update_locked_for_reconciled_payroll(self):
        payroll = SimpleNamespace(status=PayrollStatus.RECONCILED)
        self.assertEqual(len(validate_payment_date_update(payroll, {'payment_date': TOMORROW})), 1)


@mock.patch('payroll.strategies.strategy_bistp_payment.StrategyOnlinePayment.approve_for_payment_benefit_consumption')
@mock.patch('payroll.strategies.strategy_bistp_payment.StrategyOnlinePayment.get_benefits_attached_to_payroll')
class BistpPaymentDateStrategyTest(SimpleTestCase):

    def setUp(self):
        self.gateway = mock.Mock()
        StrategyBistpPayment.PAYMENT_GATEWAY = self.gateway
        self.user = SimpleNamespace(username='tester')
        self.benefit = mock.Mock(
            individual=SimpleNamespace(json_ext={'NIB': 'ST1234567890'}),
            individual_id='ind-1', amount='5000', code='B1', json_ext={}, payment_date=None,
        )

    def tearDown(self):
        StrategyBistpPayment.PAYMENT_GATEWAY = None

    def _payroll(self, payment_date):
        return mock.Mock(id='p1', payment_date=payment_date)

    def _sent_payment_date(self):
        payments = self.gateway.send_payment_batch.call_args[0][1]
        return payments[0]['payment_date']

    def test_sends_and_records_payroll_payment_date(self, get_benefits, approve):
        get_benefits.return_value.order_by.return_value = [self.benefit]
        self.gateway.send_payment_batch.return_value = [0]
        payroll = self._payroll(TOMORROW)

        StrategyBistpPayment.make_payment_for_payroll(payroll, self.user)

        self.assertEqual(self._sent_payment_date(), TOMORROW.isoformat())
        self.assertEqual(self.benefit.payment_date, TOMORROW)
        approve.assert_called_once()
        payroll.save.assert_not_called()

    def test_falls_back_to_today_and_records_on_payroll(self, get_benefits, approve):
        get_benefits.return_value.order_by.return_value = [self.benefit]
        self.gateway.send_payment_batch.return_value = [0]
        payroll = self._payroll(None)

        StrategyBistpPayment.make_payment_for_payroll(payroll, self.user)

        self.assertEqual(self._sent_payment_date(), TODAY.isoformat())
        self.assertEqual(payroll.payment_date, TODAY)
        payroll.save.assert_called_once()
        self.assertEqual(self.benefit.payment_date, TODAY)

    def test_failed_batch_does_not_record_payment_date(self, get_benefits, approve):
        get_benefits.return_value.order_by.return_value = [self.benefit]
        self.gateway.send_payment_batch.return_value = []
        payroll = self._payroll(None)

        StrategyBistpPayment.make_payment_for_payroll(payroll, self.user)

        self.assertIsNone(self.benefit.payment_date)
        self.assertIsNone(payroll.payment_date)
        payroll.save.assert_not_called()
        approve.assert_not_called()

    def test_only_benefits_of_accepted_chunks_are_approved(self, get_benefits, approve):
        other = mock.Mock(
            individual=SimpleNamespace(json_ext={'NIB': 'ST999'}),
            individual_id='ind-2', amount='5000', code='B2', json_ext={}, payment_date=None,
        )
        get_benefits.return_value.order_by.return_value = [self.benefit, other]
        self.gateway.send_payment_batch.return_value = [1]

        StrategyBistpPayment.make_payment_for_payroll(self._payroll(TOMORROW), self.user)

        approve.assert_called_once_with([other], self.user)
        self.assertEqual(other.payment_date, TOMORROW)
        self.assertIsNone(self.benefit.payment_date)
        self.assertEqual(self.benefit.json_ext.get('bistp_skip_reason'), 'batch_falhou')


class BistpTransactionIdTest(SimpleTestCase):
    PAYROLL_ID = 'df2a00df-ef7c-4c29-821c-fae4acbedc0e'

    def _chunk(self, payment_date):
        return [{'household_id': 'ind-1', 'nib_number': 'ST1', 'amount': '1500.00',
                 'payment_date': payment_date, 'currency': 'STN', 'programme_name': 'Cash Distribution'}]

    def _transaction_id(self, payment_date):
        return BistpGatewayConnector._chunk_transaction_id(self.PAYROLL_ID, self._chunk(payment_date))

    def test_transaction_id_fits_bistp_limit(self):
        self.assertLessEqual(len(self._transaction_id('2026-10-19')), 40)

    def test_same_content_keeps_transaction_id(self):
        self.assertEqual(self._transaction_id('2026-10-18'), self._transaction_id('2026-10-18'))

    def test_changed_payment_date_changes_transaction_id(self):
        self.assertNotEqual(self._transaction_id('2026-10-18'), self._transaction_id('2026-10-19'))

    def test_callback_resolves_payroll_from_transaction_id(self):
        self.assertEqual(_payroll_id_from_transaction_id(self._transaction_id('2026-10-19')), self.PAYROLL_ID)
        self.assertEqual(_payroll_id_from_transaction_id(f'{self.PAYROLL_ID}-7'), self.PAYROLL_ID)
        self.assertIsNone(_payroll_id_from_transaction_id('invalido'))
