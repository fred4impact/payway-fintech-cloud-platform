from django.urls import path

from . import views

app_name = 'risk'

urlpatterns = [
    path('held/<int:decision_id>/', views.payment_held_view, name='payment_held'),
]
