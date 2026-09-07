#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input

python manage.py migrate

python manage.py shell -c "from pheral.models import Currency; Currency.objects.get_or_create(code='NGN', defaults={'name':'Nigerian Naira','symbol':'₦','is_active':True,'decimal_places':2})"