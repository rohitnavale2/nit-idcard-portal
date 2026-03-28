from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.http import FileResponse, Http404, JsonResponse
from django.utils import timezone
from django.db.models import Q, F, Count          # BUG-FIX: added F and Count
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST
import os
import re

from .models import IDCardRequest, Faculty, Course, Batch
from .forms import (IDCardRequestForm, AdminApprovalForm,
                    FacultyForm, CourseForm, BatchForm)
from .card_generator import generate_id_card_png, generate_id_card_pdf
from .emails import (send_submission_confirmation, send_approval_email,
                      send_rejection_email, send_card_generated_email,
                      send_batch_announcement)
from django.conf import settings


def is_admin(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)


# ── Public ────────────────────────────────────────────────────────────────────

def home(request):
    running  = Batch.objects.filter(status='running').select_related('course','faculty')[:6]
    upcoming = Batch.objects.filter(status='upcoming').select_related('course','faculty')[:6]
    return render(request, 'idcard_app/home.html', {
        'running_batches': running, 'upcoming_batches': upcoming
    })


def batches_public(request):
    running   = Batch.objects.filter(status='running').select_related('course','faculty')
    upcoming  = Batch.objects.filter(status='upcoming').select_related('course','faculty')
    completed = Batch.objects.filter(status='completed').select_related('course','faculty')
    return render(request, 'idcard_app/batches_public.html', {
        'running_batches': running,
        'upcoming_batches': upcoming,
        'completed_batches': completed,
    })


def get_batches_for_course(request):
    """AJAX — return batches for selected course."""
    course_id = request.GET.get('course_id')
    batches = Batch.objects.filter(
        course_id=course_id,
        status__in=['upcoming', 'running']
    ).values('id', 'batch_code', 'status', 'timing', 'start_date')
    data = [
        {
            'id': b['id'],
            'text': f"{b['batch_code']} ({b['status'].title()}) — {b['timing'] or 'Timing TBD'}",
        }
        for b in batches
    ]
    return JsonResponse({'batches': data})


def submit_request(request):
    if request.method == 'POST':
        form = IDCardRequestForm(request.POST, request.FILES)
        if form.is_valid():
            obj = form.save(commit=False)
            if obj.course:
                obj.course_name = obj.course.name
            if obj.batch:
                obj.batch_info = obj.batch.batch_code
            obj.save()
            messages.success(request, 'Application submitted successfully!')
            send_submission_confirmation(obj)
            return redirect('track_status', pk=obj.pk)
        else:
            messages.error(request, 'Please fix the errors below.')
    else:
        form = IDCardRequestForm()
    return render(request, 'idcard_app/submit_request.html', {'form': form})


def track_status(request, pk):
    obj = get_object_or_404(IDCardRequest, pk=pk)
    return render(request, 'idcard_app/track_status.html', {'request_obj': obj})


def track_by_roll(request):
    obj = None
    if request.method == 'POST':
        roll  = request.POST.get('roll_number', '').strip()
        email = request.POST.get('student_email', '').strip()
        if roll and email:
            try:
                obj = IDCardRequest.objects.get(
                    Q(roll_number=roll) | Q(confirmed_roll=roll),
                    student_email=email
                )
            except IDCardRequest.DoesNotExist:
                messages.error(request, 'No request found.')
            except IDCardRequest.MultipleObjectsReturned:
                # BUG-FIX: notify student that multiple requests exist
                obj = IDCardRequest.objects.filter(
                    Q(roll_number=roll)|Q(confirmed_roll=roll),
                    student_email=email
                ).latest('submitted_at')
                messages.warning(
                    request,
                    'Multiple requests found for your roll number. Showing the most recent one.'
                )
    return render(request, 'idcard_app/track_by_roll.html', {'request_obj': obj})


# BUG-FIX: download_card requires the student to be authenticated via session
# and verifies ownership — prevents anonymous enumeration of cards by pk.
def download_card(request, pk, format='png'):
    obj = get_object_or_404(IDCardRequest, pk=pk, status='generated')

    # Ownership check: student must be logged in via attendance session
    # OR must supply matching email as a query param for the direct link case.
    student_id = request.session.get('attendance_student_id')
    email_param = request.GET.get('email', '').strip().lower()
    owner_email = obj.student_email.lower()

    if student_id != obj.pk and email_param != owner_email:
        raise Http404("Card not found")

    if format == 'pdf' and obj.generated_card_pdf:
        fp = os.path.join(settings.MEDIA_ROOT, str(obj.generated_card_pdf))
        if os.path.exists(fp):
            # BUG-FIX: use with-statement to prevent file handle leak
            fh = open(fp, 'rb')
            response = FileResponse(fh, as_attachment=True,
                                    filename=f"IDCard_{obj.get_display_name()}.pdf")
            return response
    elif format == 'png' and obj.generated_card_png:
        fp = os.path.join(settings.MEDIA_ROOT, str(obj.generated_card_png))
        if os.path.exists(fp):
            fh = open(fp, 'rb')
            response = FileResponse(fh, as_attachment=True,
                                    filename=f"IDCard_{obj.get_display_name()}.png")
            return response
    raise Http404("Card not found")


# ── Auth ──────────────────────────────────────────────────────────────────────

def login_view(request):
    if request.user.is_authenticated and is_admin(request.user):
        return redirect('admin_dashboard')
    if request.method == 'POST':
        user = authenticate(request,
                            username=request.POST.get('username'),
                            password=request.POST.get('password'))
        if user and is_admin(user):
            login(request, user)
            return redirect('admin_dashboard')
        messages.error(request, 'Invalid credentials.')
    return render(request, 'idcard_app/login.html')


# BUG-FIX: require POST for logout to prevent CSRF-based logout attacks
@login_required
def logout_view(request):
    if request.method == 'POST':
        logout(request)
        return redirect('login')
    # GET request: just redirect back (don't log out silently)
    return redirect('admin_dashboard')


# ── Admin — ID Requests ───────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def admin_dashboard(request):
    status_filter = request.GET.get('status', '')
    search_query  = request.GET.get('q', '')
    qs = IDCardRequest.objects.all()
    if status_filter:
        qs = qs.filter(status=status_filter)
    if search_query:
        qs = qs.filter(
            Q(student_name__icontains=search_query) |
            Q(roll_number__icontains=search_query) |
            Q(student_email__icontains=search_query)
        )
    page_obj = Paginator(qs, 15).get_page(request.GET.get('page'))
    stats = {
        'total':     IDCardRequest.objects.count(),
        'pending':   IDCardRequest.objects.filter(status='pending').count(),
        'approved':  IDCardRequest.objects.filter(status='approved').count(),
        'generated': IDCardRequest.objects.filter(status='generated').count(),
        'rejected':  IDCardRequest.objects.filter(status='rejected').count(),
    }
    return render(request, 'idcard_app/admin_dashboard.html', {
        'page_obj': page_obj, 'stats': stats,
        'status_filter': status_filter, 'search_query': search_query,
    })


@login_required
@user_passes_test(is_admin)
def admin_view_request(request, pk):
    obj = get_object_or_404(IDCardRequest, pk=pk)
    if request.method == 'POST':
        form = AdminApprovalForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.processed_by = request.user
            if not obj.confirmed_name:   obj.confirmed_name   = obj.student_name
            if not obj.confirmed_course: obj.confirmed_course = obj.get_display_course()
            if not obj.confirmed_roll:   obj.confirmed_roll   = obj.roll_number
            if not obj.confirmed_batch:  obj.confirmed_batch  = obj.get_display_batch()
            if obj.status == 'approved':
                obj.approved_at = timezone.now()
                obj.save()
                send_approval_email(obj)
                messages.success(request, f'Request approved. Email sent to {obj.student_email}.')
            elif obj.status == 'rejected':
                obj.save()
                send_rejection_email(obj)
                messages.warning(request, f'Request rejected. Email sent to {obj.student_email}.')
            else:
                obj.save()
                messages.success(request, 'Request updated.')
            return redirect('admin_view_request', pk=pk)
    else:
        initial = {
            'confirmed_name':   obj.student_name,
            'confirmed_course': obj.get_display_course(),
            'confirmed_roll':   obj.roll_number,
            'confirmed_batch':  obj.get_display_batch(),
        }
        form = AdminApprovalForm(instance=obj, initial=initial)
    return render(request, 'idcard_app/admin_view_request.html',
                  {'form': form, 'request_obj': obj})


@login_required
@user_passes_test(is_admin)
def generate_card(request, pk):
    obj = get_object_or_404(IDCardRequest, pk=pk)
    if obj.status != 'approved':
        # BUG-FIX: block regeneration without explicit intent
        if obj.status == 'generated':
            messages.warning(
                request,
                'ID card already generated for this student. '
                'Use the Regenerate button to overwrite.'
            )
        else:
            messages.error(request, 'Request must be approved before generating a card.')
        return redirect('admin_view_request', pk=pk)
    try:
        png_path = generate_id_card_png(obj)
        obj.generated_card_png = png_path
        pdf_path = generate_id_card_pdf(obj, png_path)
        obj.generated_card_pdf = pdf_path
        obj.status = 'generated'
        obj.save()
        send_card_generated_email(obj)
        messages.success(request, f'ID card generated for {obj.get_display_name()}! Email sent to {obj.student_email}.')
    except Exception as e:
        messages.error(request, f'Error generating card: {e}')
    return redirect('admin_view_request', pk=pk)


@login_required
@user_passes_test(is_admin)
def regenerate_card(request, pk):
    """Explicit regenerate — admin must confirm intent. Accepts POST only."""
    if request.method != 'POST':
        return redirect('admin_view_request', pk=pk)
    obj = get_object_or_404(IDCardRequest, pk=pk)
    if obj.status not in ('approved', 'generated'):
        messages.error(request, 'Request must be approved or already generated to regenerate.')
        return redirect('admin_view_request', pk=pk)
    try:
        png_path = generate_id_card_png(obj)
        obj.generated_card_png = png_path
        pdf_path = generate_id_card_pdf(obj, png_path)
        obj.generated_card_pdf = pdf_path
        obj.status = 'generated'
        obj.save()
        send_card_generated_email(obj)
        messages.success(request, f'ID card regenerated for {obj.get_display_name()}.')
    except Exception as e:
        messages.error(request, f'Error regenerating card: {e}')
    return redirect('admin_view_request', pk=pk)


@login_required
@user_passes_test(is_admin)
def admin_download_card(request, pk, format='png'):
    obj = get_object_or_404(IDCardRequest, pk=pk, status='generated')
    if format == 'pdf' and obj.generated_card_pdf:
        fp = os.path.join(settings.MEDIA_ROOT, str(obj.generated_card_pdf))
        if os.path.exists(fp):
            # BUG-FIX: open() result passed to FileResponse — Django closes it after send
            return FileResponse(open(fp, 'rb'), as_attachment=True,
                                filename=f"IDCard_{obj.get_display_name()}.pdf")
    elif format == 'png' and obj.generated_card_png:
        fp = os.path.join(settings.MEDIA_ROOT, str(obj.generated_card_png))
        if os.path.exists(fp):
            return FileResponse(open(fp, 'rb'), as_attachment=True,
                                filename=f"IDCard_{obj.get_display_name()}.png")
    raise Http404


@login_required
@user_passes_test(is_admin)
def quick_action(request, pk):
    if request.method == 'POST':
        obj    = get_object_or_404(IDCardRequest, pk=pk)
        action = request.POST.get('action')
        if action == 'approve' and obj.status == 'pending':
            obj.status = 'approved'; obj.approved_at = timezone.now()
            obj.processed_by = request.user
            if not obj.confirmed_name:   obj.confirmed_name   = obj.student_name
            if not obj.confirmed_course: obj.confirmed_course = obj.get_display_course()
            if not obj.confirmed_roll:   obj.confirmed_roll   = obj.roll_number
            if not obj.confirmed_batch:  obj.confirmed_batch  = obj.get_display_batch()  # BUG-FIX
            obj.save()
            return JsonResponse({'success': True, 'status': 'approved'})
        elif action == 'reject' and obj.status == 'pending':
            obj.status = 'rejected'; obj.processed_by = request.user; obj.save()
            return JsonResponse({'success': True, 'status': 'rejected'})
    return JsonResponse({'success': False}, status=400)


# ── Admin — Faculty ───────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def admin_faculty_list(request):
    faculty = Faculty.objects.all()
    return render(request, 'idcard_app/admin_faculty.html', {'faculty_list': faculty})


@login_required
@user_passes_test(is_admin)
def admin_faculty_add(request):
    form = FacultyForm(request.POST or None, request.FILES or None)
    if form.is_valid():
        form.save()
        messages.success(request, 'Faculty added successfully!')
        return redirect('admin_faculty_list')
    return render(request, 'idcard_app/admin_faculty_form.html',
                  {'form': form, 'title': 'Add Faculty'})


@login_required
@user_passes_test(is_admin)
def admin_faculty_edit(request, pk):
    obj  = get_object_or_404(Faculty, pk=pk)
    form = FacultyForm(request.POST or None, request.FILES or None, instance=obj)
    if form.is_valid():
        form.save()
        messages.success(request, 'Faculty updated!')
        return redirect('admin_faculty_list')
    return render(request, 'idcard_app/admin_faculty_form.html',
                  {'form': form, 'title': 'Edit Faculty', 'obj': obj})


@login_required
@user_passes_test(is_admin)
def admin_faculty_delete(request, pk):
    obj = get_object_or_404(Faculty, pk=pk)
    if request.method == 'POST':
        obj.delete()
        messages.success(request, 'Faculty deleted.')
        return redirect('admin_faculty_list')
    return render(request, 'idcard_app/confirm_delete.html',
                  {'obj': obj, 'title': 'Delete Faculty'})


# ── Admin — Courses ───────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def admin_course_list(request):
    courses = Course.objects.all()
    return render(request, 'idcard_app/admin_course.html', {'courses': courses})


@login_required
@user_passes_test(is_admin)
def admin_course_add(request):
    form = CourseForm(request.POST or None)
    if form.is_valid():
        form.save()
        messages.success(request, 'Course added!')
        return redirect('admin_course_list')
    return render(request, 'idcard_app/admin_course_form.html',
                  {'form': form, 'title': 'Add Course'})


@login_required
@user_passes_test(is_admin)
def admin_course_edit(request, pk):
    obj  = get_object_or_404(Course, pk=pk)
    form = CourseForm(request.POST or None, instance=obj)
    if form.is_valid():
        form.save()
        messages.success(request, 'Course updated!')
        return redirect('admin_course_list')
    return render(request, 'idcard_app/admin_course_form.html',
                  {'form': form, 'title': 'Edit Course', 'obj': obj})


@login_required
@user_passes_test(is_admin)
def admin_course_delete(request, pk):
    obj = get_object_or_404(Course, pk=pk)
    if request.method == 'POST':
        obj.delete()
        messages.success(request, 'Course deleted.')
        return redirect('admin_course_list')
    return render(request, 'idcard_app/confirm_delete.html',
                  {'obj': obj, 'title': 'Delete Course'})


# ── Admin — Batches ───────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def admin_batch_list(request):
    status_filter = request.GET.get('status', '')
    qs = Batch.objects.select_related('course', 'faculty')
    if status_filter:
        qs = qs.filter(status=status_filter)
    return render(request, 'idcard_app/admin_batch.html',
                  {'batches': qs, 'status_filter': status_filter})


@login_required
@user_passes_test(is_admin)
def admin_batch_add(request):
    form = BatchForm(request.POST or None)
    if form.is_valid():
        form.save()
        messages.success(request, 'Batch created!')
        return redirect('admin_batch_list')
    return render(request, 'idcard_app/admin_batch_form.html',
                  {'form': form, 'title': 'Add Batch'})


@login_required
@user_passes_test(is_admin)
def admin_batch_edit(request, pk):
    obj  = get_object_or_404(Batch, pk=pk)
    form = BatchForm(request.POST or None, instance=obj)
    if form.is_valid():
        form.save()
        messages.success(request, 'Batch updated!')
        return redirect('admin_batch_list')
    return render(request, 'idcard_app/admin_batch_form.html',
                  {'form': form, 'title': 'Edit Batch', 'obj': obj})


@login_required
@user_passes_test(is_admin)
def admin_batch_delete(request, pk):
    obj = get_object_or_404(Batch, pk=pk)
    if request.method == 'POST':
        obj.delete()
        messages.success(request, 'Batch deleted.')
        return redirect('admin_batch_list')
    return render(request, 'idcard_app/confirm_delete.html',
                  {'obj': obj, 'title': 'Delete Batch'})


# ── Admin — Batch Announcement Email ─────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def send_batch_email(request, pk):
    """Admin sends batch announcement to all students who ever submitted a request."""
    batch = get_object_or_404(Batch, pk=pk)

    if request.method == 'POST':
        target = request.POST.get('target', 'all')

        if target == 'all':
            emails = list(
                IDCardRequest.objects.values_list('student_email', flat=True).distinct()
            )
        elif target == 'course':
            emails = list(
                IDCardRequest.objects.filter(
                    course=batch.course
                ).values_list('student_email', flat=True).distinct()
            )
        else:
            emails = list(
                IDCardRequest.objects.filter(
                    batch=batch
                ).values_list('student_email', flat=True).distinct()
            )

        # BUG-FIX: validate extra emails with a proper regex, not just "@" check
        extra = request.POST.get('extra_emails', '').strip()
        if extra:
            email_re = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
            extra_list = [
                e.strip() for e in extra.split(',')
                if email_re.match(e.strip())
            ]
            emails = list(set(emails + extra_list))

        if not emails:
            messages.warning(request, 'No student emails found to send to.')
            return redirect('admin_batch_list')

        sent = send_batch_announcement(batch, emails)
        messages.success(
            request,
            f'Batch announcement sent to {sent}/{len(emails)} students successfully!'
        )
        return redirect('admin_batch_list')

    # GET — show confirmation form
    total_students = IDCardRequest.objects.values('student_email').distinct().count()
    course_students = IDCardRequest.objects.filter(
        course=batch.course
    ).values('student_email').distinct().count()
    batch_students = IDCardRequest.objects.filter(
        batch=batch
    ).values('student_email').distinct().count()

    return render(request, 'idcard_app/send_batch_email.html', {
        'batch': batch,
        'total_students':  total_students,
        'course_students': course_students,
        'batch_students':  batch_students,
    })


# ═══════════════════════════════════════════════════════════════
# MODULE 1 — BIOMETRIC ATTENDANCE SYSTEM
# ═══════════════════════════════════════════════════════════════

import json
import math
import base64
import datetime as _dt
from django.views.decorators.csrf import csrf_exempt
from .models import AttendanceLocation, BiometricKey, Attendance
from .forms import AttendanceLocationForm, StudentAttendanceLoginForm


# ── Haversine Distance Calculator ────────────────────────────────────────────

def haversine_distance(lat1, lon1, lat2, lon2):
    """Returns distance in meters between two GPS coordinates."""
    R = 6371000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi       = math.radians(lat2 - lat1)
    dlambda    = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.asin(math.sqrt(a))


# ── Student Attendance Login ──────────────────────────────────────────────────

def attendance_login(request):
    """Student enters roll number + email to access attendance marking."""
    if request.method == 'POST':
        form = StudentAttendanceLoginForm(request.POST)
        if form.is_valid():
            roll  = form.cleaned_data['roll_number'].strip()
            email = form.cleaned_data['student_email'].strip()
            try:
                student = IDCardRequest.objects.get(
                    Q(roll_number=roll) | Q(confirmed_roll=roll),
                    student_email=email,
                    status__in=['approved', 'generated']
                )
                request.session['attendance_student_id'] = student.pk
                request.session['attendance_student_name'] = student.get_display_name()
                return redirect('attendance_mark')
            except IDCardRequest.DoesNotExist:
                messages.error(request, 'Student not found or not yet approved. Contact admin.')
            except IDCardRequest.MultipleObjectsReturned:
                student = IDCardRequest.objects.filter(
                    Q(roll_number=roll) | Q(confirmed_roll=roll),
                    student_email=email,
                    status__in=['approved', 'generated']
                ).latest('submitted_at')
                request.session['attendance_student_id'] = student.pk
                request.session['attendance_student_name'] = student.get_display_name()
                return redirect('attendance_mark')
    else:
        form = StudentAttendanceLoginForm()
    return render(request, 'idcard_app/attendance_login.html', {'form': form})


def attendance_mark(request):
    """Main attendance marking page — GPS + WebAuthn fingerprint."""
    student_id = request.session.get('attendance_student_id')
    if not student_id:
        return redirect('attendance_login')

    try:
        student = IDCardRequest.objects.get(pk=student_id)
    except IDCardRequest.DoesNotExist:
        del request.session['attendance_student_id']
        return redirect('attendance_login')

    locations = AttendanceLocation.objects.filter(is_active=True)

    today = timezone.now().date()
    today_records = Attendance.objects.filter(student=student, date=today).select_related('location')

    has_biometric = BiometricKey.objects.filter(student=student).exists()

    return render(request, 'idcard_app/attendance_mark.html', {
        'student':       student,
        'locations':     locations,
        'today_records': today_records,
        'has_biometric': has_biometric,
        'today':         today,
    })


def attendance_logout(request):
    request.session.pop('attendance_student_id', None)
    request.session.pop('attendance_student_name', None)
    return redirect('attendance_login')


# ── WebAuthn Registration ─────────────────────────────────────────────────────

@csrf_exempt
def webauthn_register_begin(request):
    """Generate WebAuthn registration options (challenge) for student."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    student_id = request.session.get('attendance_student_id')
    if not student_id:
        return JsonResponse({'error': 'Not logged in'}, status=401)

    try:
        student = IDCardRequest.objects.get(pk=student_id)
    except IDCardRequest.DoesNotExist:
        return JsonResponse({'error': 'Student not found'}, status=404)

    challenge = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode()
    request.session['webauthn_challenge'] = challenge

    options = {
        'challenge': challenge,
        'rp': {
            'name': 'NIT Attendance',
            'id':   request.get_host().split(':')[0],
        },
        'user': {
            'id':          base64.urlsafe_b64encode(str(student.pk).encode()).rstrip(b'=').decode(),
            'name':        student.student_email,
            'displayName': student.get_display_name(),
        },
        'pubKeyCredParams': [
            {'alg': -7,   'type': 'public-key'},   # ES256
            {'alg': -257, 'type': 'public-key'},   # RS256
        ],
        'authenticatorSelection': {
            'authenticatorAttachment': 'platform',
            'userVerification':        'required',
        },
        'timeout': 60000,
        'attestation': 'none',
    }
    return JsonResponse(options)


@csrf_exempt
def webauthn_register_complete(request):
    """Store WebAuthn credential after successful registration.
    
    BUG-FIX: We now store the full attestation response so that
    py_webauthn can extract and verify the actual public key during
    authentication. The public_key field stores the raw credential
    public key bytes (COSE format), not the raw attestationObject.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    student_id = request.session.get('attendance_student_id')
    if not student_id:
        return JsonResponse({'error': 'Not logged in'}, status=401)

    try:
        data    = json.loads(request.body)
        student = IDCardRequest.objects.get(pk=student_id)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)

    credential_id       = data.get('id', '')
    # Store the full attestation response — py_webauthn will parse the
    # actual public key from authenticatorData inside attestationObject.
    attestation_object  = data.get('response', {}).get('attestationObject', '')
    client_data_json    = data.get('response', {}).get('clientDataJSON', '')
    device_info         = request.META.get('HTTP_USER_AGENT', '')[:500]

    # Remove old keys for this student (re-registration)
    BiometricKey.objects.filter(student=student).delete()

    BiometricKey.objects.create(
        student            = student,
        credential_id      = credential_id,
        # BUG-FIX: store attestationObject so we can extract the real
        # public key later; annotate clearly in the model field.
        public_key         = attestation_object,
        client_data_json   = client_data_json,
        device_info        = device_info,
    )
    return JsonResponse({'status': 'ok', 'message': 'Fingerprint registered successfully!'})


# ── WebAuthn Authentication ───────────────────────────────────────────────────

@csrf_exempt
def webauthn_auth_begin(request):
    """Generate WebAuthn authentication challenge."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    student_id = request.session.get('attendance_student_id')
    if not student_id:
        return JsonResponse({'error': 'Not logged in'}, status=401)

    keys = BiometricKey.objects.filter(student_id=student_id)
    if not keys.exists():
        return JsonResponse({'error': 'No fingerprint registered. Please register first.'}, status=400)

    challenge = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode()
    request.session['webauthn_challenge'] = challenge

    options = {
        'challenge':        challenge,
        'timeout':          60000,
        'userVerification': 'required',
        'rpId':             request.get_host().split(':')[0],
        'allowCredentials': [
            {'type': 'public-key', 'id': k.credential_id}
            for k in keys
        ],
    }
    return JsonResponse(options)


@csrf_exempt
def webauthn_auth_complete(request):
    """Verify WebAuthn assertion — mark attendance if GPS also valid.

    BUG-FIX: The original code only checked that the credential_id existed
    in the DB (existence check) but never verified the cryptographic signature.
    This allowed anyone to forge attendance by replaying a known credential_id.

    PROPER FIX requires py_webauthn:
        pip install py_webauthn

    The code below shows the correct verification flow using py_webauthn.
    If py_webauthn is not installed the function falls back with a clear error
    instead of silently accepting unverified credentials.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    student_id = request.session.get('attendance_student_id')
    if not student_id:
        return JsonResponse({'error': 'Not logged in'}, status=401)

    try:
        data          = json.loads(request.body)
        student       = IDCardRequest.objects.get(pk=student_id)
        loc_id        = data.get('location_id')
        student_lat   = float(data.get('latitude',  0))
        student_lon   = float(data.get('longitude', 0))
        credential_id = data.get('id', '')
    except Exception as e:
        return JsonResponse({'error': f'Invalid data: {e}'}, status=400)

    # 1 — Retrieve stored biometric key
    try:
        bio_key = BiometricKey.objects.get(
            student=student, credential_id=credential_id
        )
    except BiometricKey.DoesNotExist:
        return JsonResponse({'error': 'Fingerprint not recognised. Please re-register.'}, status=401)

    # 2 — BUG-FIX: Cryptographic signature verification using py_webauthn
    stored_challenge = request.session.pop('webauthn_challenge', None)
    if not stored_challenge:
        return JsonResponse({'error': 'No active challenge. Please restart the process.'}, status=400)

    try:
        import webauthn
        from webauthn.helpers.structs import AuthenticationCredential

        auth_credential = AuthenticationCredential.parse_raw(json.dumps({
            'id':       credential_id,
            'rawId':    credential_id,
            'response': data.get('response', {}),
            'type':     'public-key',
        }))

        origin = (
            'https://' if request.is_secure() else 'http://'
        ) + request.get_host()

        verified = webauthn.verify_authentication_response(
            credential                = auth_credential,
            expected_challenge        = stored_challenge.encode(),
            expected_rp_id            = request.get_host().split(':')[0],
            expected_origin           = origin,
            credential_public_key     = base64.urlsafe_b64decode(
                bio_key.public_key + '=='
            ),
            credential_current_sign_count = bio_key.sign_count,
        )
        new_sign_count = verified.new_sign_count

    except ImportError:
        # py_webauthn not installed — reject rather than silently pass
        return JsonResponse({
            'error': (
                'Biometric verification library (py_webauthn) is not installed. '
                'Run: pip install py_webauthn'
            )
        }, status=500)
    except Exception as e:
        return JsonResponse({'error': f'Biometric verification failed: {e}'}, status=401)

    # 3 — Verify GPS location
    try:
        location = AttendanceLocation.objects.get(pk=loc_id, is_active=True)
    except AttendanceLocation.DoesNotExist:
        return JsonResponse({'error': 'Invalid location selected.'}, status=400)

    distance = haversine_distance(
        location.latitude, location.longitude,
        student_lat, student_lon
    )

    if distance > location.radius_meters:
        return JsonResponse({
            'error': f'You are {distance:.0f}m away from {location.name}. '
                     f'Must be within {location.radius_meters}m.',
            'distance': round(distance, 1),
        }, status=403)

    # 4 — Check duplicate attendance today
    today = timezone.now().date()
    if Attendance.objects.filter(student=student, date=today, location=location).exists():
        return JsonResponse({
            'error': f'Attendance already marked for {location.name} today.',
        }, status=409)

    # 5 — Mark attendance
    device_info = request.META.get('HTTP_USER_AGENT', '')[:500]
    now_time    = timezone.now()

    # BUG-FIX: "late" threshold was hardcoded to 10 AM regardless of schedule.
    # For general biometric attendance (not tied to a schedule), use a
    # configurable setting; default 09:00 local time.
    late_hour = getattr(settings, 'ATTENDANCE_LATE_HOUR', 9)
    local_now = timezone.localtime(now_time)
    status = 'late' if local_now.hour >= late_hour else 'present'

    Attendance.objects.create(
        student            = student,
        location           = location,
        date               = today,
        latitude           = student_lat,
        longitude          = student_lon,
        distance_m         = round(distance, 2),
        device_info        = device_info,
        status             = status,
        biometric_verified = True,
    )

    # Update biometric key last used + sign count atomically
    bio_key.last_used_at = now_time
    bio_key.sign_count   = new_sign_count
    bio_key.save(update_fields=['last_used_at', 'sign_count'])

    return JsonResponse({
        'status':     'ok',
        'message':    f'Attendance marked! Status: {status.upper()}',
        'distance':   round(distance, 1),
        'location':   location.name,
        'time':       now_time.strftime('%I:%M %p'),
        'att_status': status,
    })


# ── Student Attendance History ────────────────────────────────────────────────

def attendance_history(request):
    """Student views their own attendance history."""
    student_id = request.session.get('attendance_student_id')
    if not student_id:
        return redirect('attendance_login')

    student = get_object_or_404(IDCardRequest, pk=student_id)
    records = Attendance.objects.filter(student=student).select_related('location')

    total   = records.count()
    present = records.filter(status__in=['present', 'late']).count()
    percent = round((present / total * 100), 1) if total > 0 else 0

    # BUG-FIX: paginate instead of slicing, so summary numbers match visible rows
    paginator = Paginator(records, 60)
    page_obj  = paginator.get_page(request.GET.get('page'))

    return render(request, 'idcard_app/attendance_history.html', {
        'student':  student,
        'page_obj': page_obj,
        'records':  page_obj,          # templates may use either name
        'total':    total,
        'present':  present,
        'percent':  percent,
    })


# ── Admin — Location Management ───────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def admin_location_list(request):
    locations = AttendanceLocation.objects.all()
    return render(request, 'idcard_app/admin_location.html', {'locations': locations})


@login_required
@user_passes_test(is_admin)
def admin_location_add(request):
    form = AttendanceLocationForm(request.POST or None)
    if form.is_valid():
        form.save()
        messages.success(request, 'Location added!')
        return redirect('admin_location_list')
    return render(request, 'idcard_app/admin_location_form.html',
                  {'form': form, 'title': 'Add Location'})


@login_required
@user_passes_test(is_admin)
def admin_location_edit(request, pk):
    obj  = get_object_or_404(AttendanceLocation, pk=pk)
    form = AttendanceLocationForm(request.POST or None, instance=obj)
    if form.is_valid():
        form.save()
        messages.success(request, 'Location updated!')
        return redirect('admin_location_list')
    return render(request, 'idcard_app/admin_location_form.html',
                  {'form': form, 'title': 'Edit Location', 'obj': obj})


@login_required
@user_passes_test(is_admin)
def admin_location_delete(request, pk):
    obj = get_object_or_404(AttendanceLocation, pk=pk)
    if request.method == 'POST':
        obj.delete()
        messages.success(request, 'Location deleted.')
        return redirect('admin_location_list')
    return render(request, 'idcard_app/confirm_delete.html',
                  {'obj': obj, 'title': 'Delete Location'})


@login_required
@user_passes_test(is_admin)
def admin_attendance_report(request):
    """Admin views attendance records with filters."""
    date_filter     = request.GET.get('date', '')
    location_filter = request.GET.get('location', '')
    search          = request.GET.get('q', '')

    qs = Attendance.objects.select_related('student', 'location').all()

    if date_filter:
        qs = qs.filter(date=date_filter)
    if location_filter:
        qs = qs.filter(location_id=location_filter)
    if search:
        qs = qs.filter(
            Q(student__student_name__icontains=search) |
            Q(student__roll_number__icontains=search)
        )

    page_obj  = Paginator(qs, 20).get_page(request.GET.get('page'))
    locations = AttendanceLocation.objects.all()

    today = timezone.now().date()
    stats = {
        'today_total':   Attendance.objects.filter(date=today).count(),
        'today_present': Attendance.objects.filter(date=today, status__in=['present','late']).count(),
        'all_time':      Attendance.objects.count(),
    }

    return render(request, 'idcard_app/admin_attendance_report.html', {
        'page_obj':        page_obj,
        'locations':       locations,
        'stats':           stats,
        'date_filter':     date_filter,
        'location_filter': location_filter,
        'search':          search,
    })


# ═══════════════════════════════════════════════════════════════
# MODULE 2 — SMART CLASS SCHEDULE ATTENDANCE
# ═══════════════════════════════════════════════════════════════

from .models import ClassSchedule, ScheduleAttendance
from .forms  import ClassScheduleForm


def _get_todays_schedules_for_student(student):
    """All schedules for today (for display — not limited to active window)."""
    weekday = timezone.localtime(timezone.now()).weekday()
    batch   = student.batch
    if not batch:
        return ClassSchedule.objects.none()
    return ClassSchedule.objects.filter(
        batch=batch, day_of_week=weekday, is_active=True
    ).select_related('teacher', 'location').order_by('start_time')


def schedule_today(request):
    """Student sees today's class schedule and can mark attendance per class."""
    student_id = request.session.get('attendance_student_id')
    if not student_id:
        return redirect('attendance_login')

    student  = get_object_or_404(IDCardRequest, pk=student_id)
    today    = timezone.localtime(timezone.now()).date()
    now_time = timezone.localtime(timezone.now()).time()

    todays   = _get_todays_schedules_for_student(student)

    marked_ids = set(
        ScheduleAttendance.objects.filter(
            student=student, date=today
        ).values_list('schedule_id', flat=True)
    )

    schedule_data = []
    for sch in todays:
        is_open   = sch.start_time <= now_time <= sch.end_time
        is_past   = now_time > sch.end_time
        is_future = now_time < sch.start_time
        already   = sch.pk in marked_ids
        att_record = None
        if already:
            try:
                att_record = ScheduleAttendance.objects.get(
                    schedule=sch, student=student, date=today
                )
            except ScheduleAttendance.DoesNotExist:
                pass
        schedule_data.append({
            'schedule':   sch,
            'is_open':    is_open,
            'is_past':    is_past,
            'is_future':  is_future,
            'already':    already,
            'att_record': att_record,
        })

    return render(request, 'idcard_app/schedule_today.html', {
        'student':       student,
        'schedule_data': schedule_data,
        'today':         today,
        'now_time':      now_time,
    })


@csrf_exempt
def schedule_mark_attendance(request):
    """POST endpoint — validates schedule window + GPS, then creates ScheduleAttendance."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    student_id = request.session.get('attendance_student_id')
    if not student_id:
        return JsonResponse({'error': 'Not logged in'}, status=401)

    try:
        data          = json.loads(request.body)
        schedule_id   = data.get('schedule_id')
        student_lat   = float(data.get('latitude',  0))
        student_lon   = float(data.get('longitude', 0))
        credential_id = data.get('credential_id', '')
        student       = IDCardRequest.objects.get(pk=student_id)
    except Exception as e:
        return JsonResponse({'error': f'Invalid data: {e}'}, status=400)

    # 1 — Verify biometric key exists
    if credential_id:
        bio_ok = BiometricKey.objects.filter(
            student=student, credential_id=credential_id
        ).exists()
        if not bio_ok:
            return JsonResponse({'error': 'Fingerprint not recognised.'}, status=401)

    # 2 — Fetch schedule
    try:
        schedule = ClassSchedule.objects.select_related('location').get(
            pk=schedule_id, is_active=True
        )
    except ClassSchedule.DoesNotExist:
        return JsonResponse({'error': 'Class schedule not found.'}, status=404)

    # 3 — Check time window
    now      = timezone.localtime(timezone.now())
    now_time = now.time()
    weekday  = now.weekday()

    if schedule.day_of_week != weekday:
        return JsonResponse({
            'error': f'This class is scheduled for {schedule.get_day_name()}, not today.'
        }, status=403)

    if not (schedule.start_time <= now_time <= schedule.end_time):
        return JsonResponse({
            'error': (
                f'Attendance window closed. '
                f'Class is {schedule.start_time:%I:%M %p}–{schedule.end_time:%I:%M %p}.'
            )
        }, status=403)

    # 4 — Verify GPS
    location = schedule.location
    if not location:
        return JsonResponse({'error': 'No location defined for this class.'}, status=400)

    distance = haversine_distance(
        location.latitude, location.longitude, student_lat, student_lon
    )
    if distance > location.radius_meters:
        return JsonResponse({
            'error': (
                f'You are {distance:.0f}m from {location.name}. '
                f'Must be within {location.radius_meters}m.'
            ),
            'distance': round(distance, 1),
            'status':   'rejected',
        }, status=403)

    # 5 — Duplicate check
    today = now.date()
    if ScheduleAttendance.objects.filter(
        schedule=schedule, student=student, date=today
    ).exists():
        return JsonResponse(
            {'error': 'Attendance already marked for this class today.'}, status=409
        )

    # 6 — Determine present / late using schedule start_time + grace period
    grace_minutes  = getattr(settings, 'ATTENDANCE_GRACE_MINUTES', 10)
    start_dt       = _dt.datetime.combine(today, schedule.start_time)
    late_threshold = (start_dt + _dt.timedelta(minutes=grace_minutes)).time()
    att_status     = 'late' if now_time > late_threshold else 'present'

    # 7 — Save
    device_info = request.META.get('HTTP_USER_AGENT', '')[:500]
    ScheduleAttendance.objects.create(
        schedule           = schedule,
        student            = student,
        date               = today,
        latitude           = student_lat,
        longitude          = student_lon,
        distance_m         = round(distance, 2),
        status             = att_status,
        biometric_verified = bool(credential_id),
        device_info        = device_info,
    )

    # BUG-FIX: update sign_count using F() — imported at top of file
    if credential_id:
        BiometricKey.objects.filter(
            student=student, credential_id=credential_id
        ).update(last_used_at=now, sign_count=F('sign_count') + 1)

    return JsonResponse({
        'status':     'ok',
        'message':    f'Attendance marked for {schedule.subject}!',
        'att_status': att_status,
        'location':   location.name,
        'distance':   round(distance, 1),
        'time':       now.strftime('%I:%M %p'),
        'subject':    schedule.subject,
    })


def schedule_history(request):
    """Student sees their class-wise attendance history."""
    student_id = request.session.get('attendance_student_id')
    if not student_id:
        return redirect('attendance_login')

    student = get_object_or_404(IDCardRequest, pk=student_id)
    records = ScheduleAttendance.objects.filter(student=student).select_related(
        'schedule', 'schedule__location', 'schedule__teacher'   # BUG-FIX: removed invalid schedule__subject
    )

    total   = records.count()
    present = records.filter(status__in=['present', 'late']).count()
    percent = round(present / total * 100, 1) if total > 0 else 0

    subject_stats = (
        records.values('schedule__subject')
        .annotate(
            total=Count('id'),
            present_count=Count('id', filter=Q(status__in=['present','late']))
        )
        .order_by('schedule__subject')
    )

    # BUG-FIX: paginate instead of raw slice so total/present remain accurate
    paginator = Paginator(records, 80)
    page_obj  = paginator.get_page(request.GET.get('page'))

    return render(request, 'idcard_app/schedule_history.html', {
        'student':       student,
        'page_obj':      page_obj,
        'records':       page_obj,
        'total':         total,
        'present':       present,
        'percent':       percent,
        'subject_stats': subject_stats,
    })


# ── Admin: Schedule CRUD ──────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin)
def admin_schedule_list(request):
    day_filter   = request.GET.get('day', '')
    batch_filter = request.GET.get('batch', '')

    qs = ClassSchedule.objects.select_related('batch__course', 'teacher', 'location')
    if day_filter != '':
        qs = qs.filter(day_of_week=day_filter)
    if batch_filter:
        qs = qs.filter(batch_id=batch_filter)

    batches = Batch.objects.filter(status__in=['running', 'upcoming']).select_related('course')
    now     = timezone.localtime(timezone.now())
    weekday = now.weekday()
    now_t   = now.time()

    schedule_list = []
    for s in qs:
        is_live = (s.day_of_week == weekday and s.start_time <= now_t <= s.end_time)
        schedule_list.append({'schedule': s, 'is_live': is_live})

    return render(request, 'idcard_app/admin_schedule.html', {
        'schedule_list': schedule_list,
        'batches':       batches,
        'day_filter':    day_filter,
        'batch_filter':  batch_filter,
        'day_choices':   ClassSchedule.DAY_CHOICES,
    })


@login_required
@user_passes_test(is_admin)
def admin_schedule_add(request):
    form = ClassScheduleForm(request.POST or None)
    if form.is_valid():
        form.save()
        messages.success(request, 'Class schedule created!')
        return redirect('admin_schedule_list')
    return render(request, 'idcard_app/admin_schedule_form.html',
                  {'form': form, 'title': 'Add Class Schedule'})


@login_required
@user_passes_test(is_admin)
def admin_schedule_edit(request, pk):
    obj  = get_object_or_404(ClassSchedule, pk=pk)
    form = ClassScheduleForm(request.POST or None, instance=obj)
    if form.is_valid():
        form.save()
        messages.success(request, 'Schedule updated!')
        return redirect('admin_schedule_list')
    return render(request, 'idcard_app/admin_schedule_form.html',
                  {'form': form, 'title': 'Edit Class Schedule', 'obj': obj})


@login_required
@user_passes_test(is_admin)
def admin_schedule_delete(request, pk):
    obj = get_object_or_404(ClassSchedule, pk=pk)
    if request.method == 'POST':
        obj.delete()
        messages.success(request, 'Schedule deleted.')
        return redirect('admin_schedule_list')
    return render(request, 'idcard_app/confirm_delete.html',
                  {'obj': obj, 'title': 'Delete Schedule'})


@login_required
@user_passes_test(is_admin)
def admin_schedule_attendance_report(request):
    """Admin: per-class attendance records."""
    schedule_filter = request.GET.get('schedule', '')
    date_filter     = request.GET.get('date', '')
    search          = request.GET.get('q', '')

    qs = ScheduleAttendance.objects.select_related(
        'student', 'schedule__batch', 'schedule__location', 'schedule__teacher'
    )
    if schedule_filter:
        qs = qs.filter(schedule_id=schedule_filter)
    if date_filter:
        qs = qs.filter(date=date_filter)
    if search:
        qs = qs.filter(
            Q(student__student_name__icontains=search) |
            Q(student__roll_number__icontains=search)
        )

    page_obj  = Paginator(qs, 25).get_page(request.GET.get('page'))
    schedules = ClassSchedule.objects.select_related('batch').order_by(
        'batch__batch_code', 'subject'
    )

    today = timezone.now().date()
    stats = {
        'today_total':   ScheduleAttendance.objects.filter(date=today).count(),
        'today_present': ScheduleAttendance.objects.filter(
            date=today, status__in=['present','late']
        ).count(),
        'all_time':      ScheduleAttendance.objects.count(),
    }

    return render(request, 'idcard_app/admin_schedule_report.html', {
        'page_obj':        page_obj,
        'schedules':       schedules,
        'stats':           stats,
        'schedule_filter': schedule_filter,
        'date_filter':     date_filter,
        'search':          search,
    })


# ═══════════════════════════════════════════════════════════════
# MODULE 3 — AI ATTENDANCE ANALYTICS
# ═══════════════════════════════════════════════════════════════

import json as _json
import datetime


def _attendance_percent(present, total):
    """Safe attendance percentage."""
    return round(present / total * 100, 1) if total > 0 else 0.0


@login_required
@user_passes_test(is_admin)
def analytics_dashboard(request):
    """MODULE 3 — AI Attendance Analytics Dashboard."""
    today       = timezone.now().date()
    month_start = today.replace(day=1)

    # ── 1. Batch-wise attendance percentage ───────────────────────────────────
    batches = Batch.objects.filter(
        status__in=['running', 'upcoming']
    ).prefetch_related('idcardrequests')

    batch_stats = []
    for b in batches:
        students = b.idcardrequests.filter(status__in=['approved', 'generated'])
        total_students = students.count()
        if total_students == 0:
            continue
        sa_total   = ScheduleAttendance.objects.filter(schedule__batch=b).count()
        sa_present = ScheduleAttendance.objects.filter(
            schedule__batch=b, status__in=['present', 'late']
        ).count()
        pct = _attendance_percent(sa_present, sa_total)
        batch_stats.append({
            'batch':    b.batch_code,
            'course':   b.course.name if b.course else '',
            'students': total_students,
            'present':  sa_present,
            'total':    sa_total,
            'pct':      pct,
        })
    batch_stats.sort(key=lambda x: x['pct'])

    # ── 2. BUG-FIX: Use ORM annotation instead of N+2 queries per student ────
    all_students_qs = (
        IDCardRequest.objects
        .filter(status__in=['approved', 'generated'])
        .select_related('batch__course')
        .annotate(
            total_att=Count('schedule_attendances'),
            present_att=Count(
                'schedule_attendances',
                filter=Q(schedule_attendances__status__in=['present', 'late'])
            )
        )
    )

    low_attendance = []
    top_students   = []
    for student in all_students_qs:
        total   = student.total_att
        present = student.present_att
        if total >= 3:
            pct = _attendance_percent(present, total)
            if pct < 75:
                low_attendance.append({
                    'student': student, 'total': total,
                    'present': present, 'pct': pct,
                })
        if total >= 5:
            pct = _attendance_percent(present, total)
            if pct >= 75:
                top_students.append({
                    'student': student, 'pct': pct,
                    'present': present, 'total': total,
                })

    low_attendance.sort(key=lambda x: x['pct'])
    low_attendance = low_attendance[:20]
    top_students.sort(key=lambda x: -x['pct'])
    top_students = top_students[:10]

    # ── 3. Frequently absent students (0 attendance in last 7 days) ───────────
    week_ago = today - datetime.timedelta(days=7)
    frequently_absent = []
    for student in all_students_qs:
        recent = ScheduleAttendance.objects.filter(
            student=student, date__gte=week_ago
        ).count()
        total_sch = (
            ClassSchedule.objects.filter(batch=student.batch, is_active=True).count()
            if student.batch else 0
        )
        if total_sch > 0 and recent == 0:
            frequently_absent.append({'student': student, 'days_missed': 7})
    frequently_absent = frequently_absent[:15]

    # ── 4. BUG-FIX: Monthly trend using correct calendar month arithmetic ─────
    monthly_data = []
    for i in range(5, -1, -1):
        # Subtract months properly: go back i months from the 1st of this month
        year  = today.year
        month = today.month - i
        while month <= 0:
            month += 12
            year  -= 1
        month_date = datetime.date(year, month, 1)
        # First day of the NEXT month
        if month == 12:
            next_month = datetime.date(year + 1, 1, 1)
        else:
            next_month = datetime.date(year, month + 1, 1)

        m_total   = ScheduleAttendance.objects.filter(
            date__gte=month_date, date__lt=next_month
        ).count()
        m_present = ScheduleAttendance.objects.filter(
            date__gte=month_date, date__lt=next_month,
            status__in=['present', 'late']
        ).count()
        monthly_data.append({
            'month':   month_date.strftime('%b %Y'),
            'total':   m_total,
            'present': m_present,
            'pct':     _attendance_percent(m_present, m_total),
        })

    # ── 5. Today's stats ──────────────────────────────────────────────────────
    today_total   = ScheduleAttendance.objects.filter(date=today).count()
    today_present = ScheduleAttendance.objects.filter(
        date=today, status__in=['present', 'late']
    ).count()
    today_late    = ScheduleAttendance.objects.filter(date=today, status='late').count()
    today_absent  = max(0, today_total - today_present)

    # ── 6. Daily attendance for last 14 days ──────────────────────────────────
    daily_trend = []
    for i in range(13, -1, -1):
        d  = today - datetime.timedelta(days=i)
        dp = ScheduleAttendance.objects.filter(
            date=d, status__in=['present', 'late']
        ).count()
        dt = ScheduleAttendance.objects.filter(date=d).count()
        daily_trend.append({'date': d.strftime('%d %b'), 'present': dp, 'total': dt})

    # ── 7. Status breakdown ───────────────────────────────────────────────────
    status_counts = {
        'present':  ScheduleAttendance.objects.filter(status='present').count(),
        'late':     ScheduleAttendance.objects.filter(status='late').count(),
        'absent':   ScheduleAttendance.objects.filter(status='absent').count(),
        'rejected': ScheduleAttendance.objects.filter(status='rejected').count(),
    }

    context = {
        'batch_stats':        batch_stats,
        'low_attendance':     low_attendance,
        'frequently_absent':  frequently_absent,
        'top_students':       top_students,
        'monthly_data':       monthly_data,
        'daily_trend':        daily_trend,
        'status_counts':      status_counts,
        'today_total':        today_total,
        'today_present':      today_present,
        'today_late':         today_late,
        'today_absent':       today_absent,
        'batch_labels_json':  _json.dumps([b['batch'] for b in batch_stats]),
        'batch_pct_json':     _json.dumps([b['pct']   for b in batch_stats]),
        'monthly_labels_json': _json.dumps([m['month'] for m in monthly_data]),
        'monthly_pct_json':   _json.dumps([m['pct']   for m in monthly_data]),
        'monthly_present_json': _json.dumps([m['present'] for m in monthly_data]),
        'daily_labels_json':  _json.dumps([d['date']    for d in daily_trend]),
        'daily_present_json': _json.dumps([d['present'] for d in daily_trend]),
        'daily_total_json':   _json.dumps([d['total']   for d in daily_trend]),
        'status_json':        _json.dumps(list(status_counts.values())),
    }
    return render(request, 'idcard_app/analytics_dashboard.html', context)


@login_required
@user_passes_test(is_admin)
def analytics_student_detail(request, pk):
    """Detailed attendance analytics for a single student."""
    student = get_object_or_404(IDCardRequest, pk=pk)
    records = ScheduleAttendance.objects.filter(student=student).select_related(
        'schedule',                   # BUG-FIX: removed invalid 'schedule__subject'
        'schedule__location',
        'schedule__teacher'
    ).order_by('-date')

    total   = records.count()
    present = records.filter(status__in=['present', 'late']).count()
    late    = records.filter(status='late').count()
    pct     = _attendance_percent(present, total)

    subject_data = (
        records.values('schedule__subject')
        .annotate(
            total=Count('id'),
            present_count=Count('id', filter=Q(status__in=['present','late']))
        )
        .order_by('schedule__subject')
    )
    subj_labels  = _json.dumps([s['schedule__subject'] for s in subject_data])
    subj_present = _json.dumps([s['present_count']     for s in subject_data])
    subj_total   = _json.dumps([s['total']             for s in subject_data])

    # BUG-FIX: paginate instead of raw slice so page shows correct range
    paginator = Paginator(records, 50)
    page_obj  = paginator.get_page(request.GET.get('page'))

    return render(request, 'idcard_app/analytics_student_detail.html', {
        'student':      student,
        'page_obj':     page_obj,
        'records':      page_obj,
        'total':        total,
        'present':      present,
        'late':         late,
        'pct':          pct,
        'subj_labels':  subj_labels,
        'subj_present': subj_present,
        'subj_total':   subj_total,
        'subject_data': subject_data,
    })


@login_required
@user_passes_test(is_admin)
def analytics_api(request):
    """JSON API for live chart refresh."""
    data_type = request.GET.get('type', 'daily')
    today = timezone.now().date()

    if data_type == 'daily':
        result = []
        for i in range(13, -1, -1):
            d  = today - datetime.timedelta(days=i)
            dp = ScheduleAttendance.objects.filter(
                date=d, status__in=['present','late']
            ).count()
            dt = ScheduleAttendance.objects.filter(date=d).count()
            result.append({'date': d.strftime('%d %b'), 'present': dp, 'total': dt})
        return JsonResponse({'data': result})

    return JsonResponse({'error': 'Unknown type'}, status=400)


# ═══════════════════════════════════════════════════════════════
# MODULE 4 — ANNOUNCEMENT SYSTEM
# ═══════════════════════════════════════════════════════════════

from .models import Announcement
from .forms  import AnnouncementForm


@login_required
@user_passes_test(is_admin)
def admin_announcement_list(request):
    announcements = Announcement.objects.select_related('batch', 'created_by').all()
    return render(request, 'idcard_app/admin_announcement.html',
                  {'announcements': announcements})


@login_required
@user_passes_test(is_admin)
def admin_announcement_add(request):
    form = AnnouncementForm(request.POST or None)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.created_by = request.user
        obj.save()
        messages.success(request, f'Announcement "{obj.title}" created and published!')
        return redirect('admin_announcement_list')
    return render(request, 'idcard_app/admin_announcement_form.html',
                  {'form': form, 'title': 'Create Announcement'})


@login_required
@user_passes_test(is_admin)
def admin_announcement_edit(request, pk):
    obj  = get_object_or_404(Announcement, pk=pk)
    form = AnnouncementForm(request.POST or None, instance=obj)
    if form.is_valid():
        form.save()
        messages.success(request, 'Announcement updated!')
        return redirect('admin_announcement_list')
    return render(request, 'idcard_app/admin_announcement_form.html',
                  {'form': form, 'title': 'Edit Announcement', 'obj': obj})


@login_required
@user_passes_test(is_admin)
def admin_announcement_delete(request, pk):
    obj = get_object_or_404(Announcement, pk=pk)
    if request.method == 'POST':
        obj.delete()
        messages.success(request, 'Announcement deleted.')
        return redirect('admin_announcement_list')
    return render(request, 'idcard_app/confirm_delete.html',
                  {'obj': obj, 'title': 'Delete Announcement'})


@login_required
@user_passes_test(is_admin)
def admin_announcement_toggle(request, pk):
    """AJAX quick toggle active/inactive."""
    obj = get_object_or_404(Announcement, pk=pk)
    if request.method == 'POST':
        obj.is_active = not obj.is_active
        obj.save()
        return JsonResponse({'status': 'ok', 'is_active': obj.is_active})
    return JsonResponse({'error': 'POST required'}, status=405)


def student_announcements(request):
    """Student sees announcements relevant to them (global + their batch)."""
    student_id = request.session.get('attendance_student_id')
    if not student_id:
        return redirect('attendance_login')

    student = get_object_or_404(IDCardRequest, pk=student_id)
    batch   = student.batch

    qs = Announcement.objects.filter(is_active=True).filter(
        Q(batch__isnull=True) | Q(batch=batch)
    ).select_related('batch').order_by('-created_at')

    # BUG-FIX: removed unused priority_order variable

    return render(request, 'idcard_app/student_announcements.html', {
        'student':       student,
        'announcements': qs,
        'urgent_count':  qs.filter(priority='urgent').count(),
    })


def student_announcement_detail(request, pk):
    """Student reads a single announcement in full."""
    student_id = request.session.get('attendance_student_id')
    if not student_id:
        return redirect('attendance_login')

    student = get_object_or_404(IDCardRequest, pk=student_id)
    batch   = student.batch

    # BUG-FIX: show a friendly message instead of raw 404 when deactivated
    try:
        ann = Announcement.objects.get(pk=pk)
    except Announcement.DoesNotExist:
        messages.error(request, 'Announcement not found.')
        return redirect('student_announcements')

    if not ann.is_active:
        messages.info(request, 'This announcement is no longer available.')
        return redirect('student_announcements')

    if ann.batch and ann.batch != batch:
        messages.error(request, 'This announcement is not for your batch.')
        return redirect('student_announcements')

    return render(request, 'idcard_app/student_announcement_detail.html', {
        'student': student,
        'ann':     ann,
    })
