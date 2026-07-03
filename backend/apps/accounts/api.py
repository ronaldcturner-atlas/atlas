from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework import status

from .models import Physician
from .serializers import PhysicianSerializer, UserSerializer


class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        return


@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    """
    Login endpoint. Expects username and password in request body.
    """
    username = request.data.get('username')
    password = request.data.get('password')
    
    if not username or not password:
        return Response(
            {'error': 'Username and password are required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    user = authenticate(request, username=username, password=password)
    if user is not None:
        login(request, user)
        serializer = UserSerializer(user)
        return Response(serializer.data, status=status.HTTP_200_OK)
    else:
        return Response(
            {'error': 'Invalid username or password'},
            status=status.HTTP_401_UNAUTHORIZED
        )


@api_view(['POST'])
@permission_classes([AllowAny])
def logout_view(request):
    """
    Logout endpoint. Clears the session.
    """
    logout(request)
    return Response({'status': 'logged out'}, status=status.HTTP_200_OK)


# Apply csrf_exempt to logout_view
logout_view = csrf_exempt(logout_view)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me_view(request):
    """
    Get current authenticated user info.
    """
    serializer = UserSerializer(request.user)
    return Response(serializer.data)


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def physicians_list_create(request):
    if request.method == 'GET':
        physicians = Physician.objects.select_related('user', 'primary_facility').all()
        serializer = PhysicianSerializer(physicians, many=True)
        return Response(serializer.data)

    serializer = PhysicianSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def physician_detail(request, physician_id):
    physician = get_object_or_404(
        Physician.objects.select_related('user', 'primary_facility'),
        id=physician_id,
    )

    if request.method == 'GET':
        serializer = PhysicianSerializer(physician)
        return Response(serializer.data)

    partial = request.method == 'PATCH'
    serializer = PhysicianSerializer(physician, data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def physician_disable(request, physician_id):
    physician = get_object_or_404(Physician, id=physician_id)
    physician.active = False
    physician.save(update_fields=['active'])
    serializer = PhysicianSerializer(physician)
    return Response(serializer.data)
