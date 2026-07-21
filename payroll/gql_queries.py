import graphene
from django.db.models import Sum, Q, Count, Case, When, IntegerField
from graphene_django import DjangoObjectType

from core import prefix_filterset, ExtendedConnection
from core.gql_queries import UserGQLType
from core.utils import DefaultStorageFileHandler
from invoice.gql.gql_types.bill_types import BillGQLType
from location.gql_queries import LocationGQLType
from individual.gql_queries import IndividualGQLType
from payroll.models import PaymentPoint, Payroll, BenefitConsumption, \
    PayrollBenefitConsumption, BenefitAttachment, CsvReconciliationUpload
from contribution_plan.gql import PaymentPlanGQLType
from payment_cycle.gql_queries import PaymentCycleGQLType
from social_protection.models import BenefitPlan


class PaymentPointGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = PaymentPoint
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "name": ["iexact", "istartswith", "icontains"],
            **prefix_filterset("location__", LocationGQLType._meta.filter_fields),
            **prefix_filterset("ppm__", UserGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class BenefitAttachmentGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = BenefitAttachment
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            **prefix_filterset("bill__", BillGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class BenefitConsumptionGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    benefit_attachment = graphene.List(BenefitAttachmentGQLType)

    class Meta:
        model = BenefitConsumption
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "photo": ["iexact", "istartswith", "icontains"],
            "code": ["iexact", "istartswith", "icontains"],
            "status": ["exact", "startswith", "icontains", "contains"],
            "receipt": ["exact", "startswith", "icontains"],
            "type": ["exact", "startswith", "icontains"],
            "amount": ["exact", "lt", "lte", "gt", "gte"],
            "date_due": ["exact", "lt", "lte", "gt", "gte"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_benefit_attachment(self, info):
        # Use prefetch cache when available (set by PayrollGQLType.resolve_benefit_consumption)
        if hasattr(self, '_prefetched_objects_cache') and 'benefitattachment_set' in self._prefetched_objects_cache:
            return [a for a in self.benefitattachment_set.all() if not a.is_deleted]
        return BenefitAttachment.objects.filter(benefit_id=self.id, is_deleted=False)


class BistpPayrollSummaryGQLType(graphene.ObjectType):
    reconciled  = graphene.Int()
    rejected    = graphene.Int()
    pending     = graphene.Int()
    skipped_nib = graphene.Int()
    send_failed = graphene.Int()
    total       = graphene.Int()


class PayrollGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    benefit_consumption = graphene.List(BenefitConsumptionGQLType)
    benefit_plan_name_code = graphene.String()
    bistp_summary = graphene.Field(BistpPayrollSummaryGQLType)

    class Meta:
        model = Payroll
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "name": ["iexact", "istartswith", "icontains"],
            "status": ["exact", "startswith", "contains"],
            "payment_method": ["exact", "startswith", "contains"],
            **prefix_filterset("payment_point__", PaymentPointGQLType._meta.filter_fields),
            **prefix_filterset("payment_plan__", PaymentPlanGQLType._meta.filter_fields),
            **prefix_filterset("payment_cycle__", PaymentCycleGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_benefit_consumption(self, info):
        return (
            BenefitConsumption.objects
            .filter(
                payrollbenefitconsumption__payroll__id=self.id,
                is_deleted=False,
                payrollbenefitconsumption__is_deleted=False,
            )
            .select_related('individual')
            .prefetch_related('benefitattachment_set__bill')
        )

    def resolve_benefit_plan_name_code(self, info):
        benefit_plan = BenefitPlan.objects.get(id=self.payment_plan.benefit_plan.id, is_deleted=False)
        return f"{benefit_plan.code} - {benefit_plan.name}"

    def resolve_bistp_summary(self, info):
        # Single query with conditional aggregation instead of 6 separate COUNTs
        agg = BenefitConsumption.objects.filter(
            payrollbenefitconsumption__payroll_id=self.id,
            is_deleted=False,
        ).aggregate(
            reconciled=Count(Case(When(status='RECONCILED', then=1), output_field=IntegerField())),
            rejected=Count(Case(When(status='REJECTED', then=1), output_field=IntegerField())),
            pending=Count(Case(When(status='APPROVE_FOR_PAYMENT', then=1), output_field=IntegerField())),
            skipped_nib=Count(Case(
                When(status='ACCEPTED', json_ext__bistp_skip_reason='nib_ausente', then=1),
                output_field=IntegerField(),
            )),
            send_failed=Count(Case(
                When(status='ACCEPTED', json_ext__bistp_skip_reason='envio_falhou', then=1),
                output_field=IntegerField(),
            )),
            total=Count('id'),
        )
        return BistpPayrollSummaryGQLType(**agg)


class PaymentMethodGQLType(graphene.ObjectType):
    name = graphene.String()


class PaymentGatewayConfigGQLType(graphene.ObjectType):
    base_url = graphene.String()
    api_key = graphene.String()
    timeout = graphene.Int()


class PaymentMethodListGQLType(graphene.ObjectType):
    payment_methods = graphene.List(PaymentMethodGQLType)


class BenefitAttachmentListGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = BenefitAttachment
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            **prefix_filterset("bill__", BillGQLType._meta.filter_fields),
            **prefix_filterset("benefit__", BenefitConsumptionGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class CsvReconciliationUploadGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = CsvReconciliationUpload
        interfaces = (graphene.relay.Node,)

        filter_fields = {
            "id": ["exact"],
            "file_name": ["exact", "iexact", "istartswith", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "status": ["exact", "iexact", "istartswith", "icontains"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("payroll__", PayrollGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class PayrollBenefitConsumptionGQLType(DjangoObjectType):

    class Meta:
        model = PayrollBenefitConsumption
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            **prefix_filterset("payroll__", PayrollGQLType._meta.filter_fields),
            **prefix_filterset("benefit__", BenefitConsumptionGQLType._meta.filter_fields),
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class BenefitsSummaryGQLType(graphene.ObjectType):
    total_amount_received = graphene.String()
    total_amount_due = graphene.String()
