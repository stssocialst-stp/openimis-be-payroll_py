import core.fields
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0022_alter_csvreconciliationupload_user_created_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="payroll",
            name="payment_date",
            field=core.fields.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="historicalpayroll",
            name="payment_date",
            field=core.fields.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="benefitconsumption",
            name="payment_date",
            field=core.fields.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="historicalbenefitconsumption",
            name="payment_date",
            field=core.fields.DateField(blank=True, null=True),
        ),
    ]
