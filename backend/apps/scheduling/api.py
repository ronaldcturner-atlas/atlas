from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import Shift
from .serializers import ShiftSerializer


@api_view(['GET'])
def shifts_list(request):
    """
    Get all shifts.
    """
    shifts = Shift.objects.all()
    serializer = ShiftSerializer(shifts, many=True)
    return Response(serializer.data)
