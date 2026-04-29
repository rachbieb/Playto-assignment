from django.urls import path

from . import views


urlpatterns = [
    path("merchants", views.merchants),
    path("dashboard", views.dashboard),
    path("payouts", views.payouts),
    path("credits/simulate", views.simulate_credit),
    path("worker/process-batch", views.process_batch),
]
