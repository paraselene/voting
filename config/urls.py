from django.urls import path
from election import views

urlpatterns = [path('', views.login, name='login'), path('logout/', views.logout, name='logout'), path('vote/', views.vote, name='vote'), path('manage/', views.dashboard, name='dashboard'), path('credentials/', views.credentials, name='credentials')]
