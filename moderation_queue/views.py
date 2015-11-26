import bleach
import os
import re
from os.path import join
from tempfile import NamedTemporaryFile

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.sites.models import Site
from django.contrib import messages
from django.core.mail import send_mail
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.shortcuts import render, get_object_or_404
from django.template.loader import render_to_string
from django.utils.http import urlquote
from django.utils.translation import ugettext as _
from django.views.generic import ListView, TemplateView
from django.contrib.contenttypes.models import ContentType
from django.core.files.storage import FileSystemStorage

from PIL import Image as PillowImage

from auth_helpers.views import GroupRequiredMixin
from candidates.management.images import get_file_md5sum

from .forms import UploadPersonPhotoForm, PhotoReviewForm
from .models import QueuedImage, ImageExtra, PHOTO_REVIEWERS_GROUP_NAME

from candidates.models import LoggedAction
from candidates.views.version_data import get_client_ip, get_change_metadata

from popolo.models import Person
from images.models import Image

@login_required
def upload_photo(request, popit_person_id):
    person = get_object_or_404(Person, id=popit_person_id)
    if request.method == 'POST':
        form = UploadPersonPhotoForm(request.POST, request.FILES)
        if form.is_valid():
            # Make sure that we save the user that made the upload
            queued_image = form.save(commit=False)
            queued_image.user = request.user
            queued_image.save()
            # Record that action:
            LoggedAction.objects.create(
                user=request.user,
                action_type='photo-upload',
                ip_address=get_client_ip(request),
                popit_person_new_version='',
                person=person,
                source=form.cleaned_data['justification_for_use'],
            )
            return HttpResponseRedirect(reverse(
                'photo-upload-success',
                kwargs={
                    'popit_person_id': person.id
                }
            ))
    else:
        form = UploadPersonPhotoForm(
            initial={
                'person': person
            }
        )
    return render(
        request,
        'moderation_queue/photo-upload-new.html',
        {'form': form,
         'queued_images': QueuedImage.objects.filter(
             person=person,
             decision='undecided',
         ).order_by('created'),
         'person': person}
    )


class PhotoUploadSuccess(TemplateView):
    template_name = 'moderation_queue/photo-upload-success.html'

    def get_context_data(self, **kwargs):
        context = super(PhotoUploadSuccess, self).get_context_data(**kwargs)
        context['person'] = Person.objects.get(
            id=kwargs['popit_person_id']
        )
        return context


class PhotoReviewList(GroupRequiredMixin, ListView):
    template_name = 'moderation_queue/photo-review-list.html'
    required_group_name = PHOTO_REVIEWERS_GROUP_NAME

    def get_queryset(self):
        return QueuedImage.objects. \
            filter(decision='undecided'). \
            order_by('created')


def tidy_party_name(name):
    """If a party name contains an initialism in brackets, use that instead

    >>> tidy_party_name('Hello World Party (HWP)')
    'HWP'
    >>> tidy_party_name('Hello World Party')
    'Hello World Party'
    """
    m = re.search(r'\(([A-Z]+)\)', name)
    if m:
        return m.group(1)
    return name


def value_if_none(v, default):
    return default if v is None else v


class PhotoReview(GroupRequiredMixin, TemplateView):
    """The class-based view for approving or rejecting a particular photo"""

    template_name = 'moderation_queue/photo-review.html'
    http_method_names = ['get', 'post']
    required_group_name = PHOTO_REVIEWERS_GROUP_NAME

    def get_google_image_search_url(self, person):
        image_search_query = u'"{0}"'.format(person.name)
        if person.extra.last_party():
            image_search_query += u' "{0}"'.format(
                tidy_party_name(person.extra.last_party().name)
            )
        cons_2015 = person.extra.standing_in('2015')
        if cons_2015 is not None:
            image_search_query += u' "{0}"'.format(cons_2015.area.name)
        return u'https://www.google.co.uk/search?tbm=isch&q={0}'.format(
            urlquote(image_search_query)
        )

    def get_google_reverse_image_search_url(self, image_url):
        url = 'https://www.google.com/searchbyimage?&image_url='
        absolute_image_url = self.request.build_absolute_uri(image_url)
        return url + urlquote(absolute_image_url)

    def get_context_data(self, **kwargs):
        context = super(PhotoReview, self).get_context_data(**kwargs)
        self.queued_image = get_object_or_404(
            QueuedImage,
            pk=kwargs['queued_image_id']
        )
        context['queued_image'] = self.queued_image
        person = Person.objects.get(
            id=self.queued_image.person.id,
        )
        context['has_crop_bounds'] = int(self.queued_image.has_crop_bounds)
        max_x = self.queued_image.image.width - 1
        max_y = self.queued_image.image.height - 1
        guessed_crop_bounds = [
            value_if_none(self.queued_image.crop_min_x, 0),
            value_if_none(self.queued_image.crop_min_y, 0),
            value_if_none(self.queued_image.crop_max_x, max_x),
            value_if_none(self.queued_image.crop_max_y, max_y),
        ]
        context['form'] = PhotoReviewForm(
            initial = {
                'queued_image_id': self.queued_image.id,
                'decision': self.queued_image.decision,
                'x_min': guessed_crop_bounds[0],
                'y_min': guessed_crop_bounds[1],
                'x_max': guessed_crop_bounds[2],
                'y_max': guessed_crop_bounds[3],
                'moderator_why_allowed': self.queued_image.why_allowed,
                'make_primary': True,
            }
        )
        context['guessed_crop_bounds'] = guessed_crop_bounds
        context['why_allowed'] = self.queued_image.why_allowed
        context['moderator_why_allowed'] = self.queued_image.why_allowed
        # There are often source links supplied in the justification,
        # and it's convenient to be able to follow them. However, make
        # sure that any maliciously added HTML tags have been stripped
        # before linkifying any URLs:
        context['justification_for_use'] = \
            bleach.linkify(
                bleach.clean(
                    self.queued_image.justification_for_use,
                    tags=[],
                    strip=True
                )
            )
        context['google_image_search_url'] = self.get_google_image_search_url(
            person
        )
        context['google_reverse_image_search_url'] = \
            self.get_google_reverse_image_search_url(
                self.queued_image.image.url
        )
        context['person'] = person
        return context

    def send_mail(self, subject, message, email_support_too=False):
        recipients = [self.queued_image.user.email]
        if email_support_too:
            recipients.append(settings.SUPPORT_EMAIL)
        return send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=False,
        )

    def crop_and_upload_image_to_popit(self, image_filename, crop_bounds, moderator_why_allowed, make_primary):
        image_storage = FileSystemStorage()
        original = PillowImage.open(image_filename)
        # Some uploaded images are CYMK, which gives you an error when
        # you try to write them as PNG, so convert to RGBA (this is
        # RGBA rather than RGB so that any alpha channel (transparency)
        # is preserved).
        person_id = self.queued_image.person.id
        original = original.convert('RGBA')
        cropped = original.crop(crop_bounds)
        ntf = NamedTemporaryFile(delete=False)
        cropped.save(ntf.name, 'PNG')
        filename = str(person_id) + '.png'
        storage_filename = image_storage.get_available_name(filename)
        storage_path = join('images', storage_filename)
        with open(ntf.name, 'rb') as f:
            storage_filename = image_storage.save(
                storage_path, f
            )
        # Upload the image to PopIt...
        person = Person.objects.get(id=person_id)
        person_extra_content_type = ContentType.objects.get_for_model(person.extra)

        source = _('Uploaded by {uploaded_by}: Approved from photo moderation queue') \
            .format(
                uploaded_by=self.queued_image.user.username,
            )

        image = Image.objects.create(
            image=storage_filename,
            source=source,
            is_primary=make_primary,
            object_id=person.extra.id,
            content_type_id=person_extra_content_type.id
        )

        extra = ImageExtra.objects.create(
            base=image,
            uploading_user=self.queued_image.user,
            user_notes=self.queued_image.justification_for_use,
            copyright=self.queued_image.why_allowed
        )

        data = {
            'md5sum': get_file_md5sum(ntf.name),
            'user_why_allowed': self.queued_image.why_allowed,
            'user_justification_for_use': self.queued_image.justification_for_use,
            'moderator_why_allowed': moderator_why_allowed,
            'mime_type': 'image/png',
            'notes': _('Approved from photo moderation queue'),
            'uploaded_by_user': self.queued_image.user.username,
            'created': None,
        }
        os.remove(ntf.name)

    def form_valid(self, form):
        decision = form.cleaned_data['decision']
        person = Person.objects.get(
            id=self.queued_image.person.id
        )
        candidate_path = person.extra.get_absolute_url()
        candidate_name = person.name
        candidate_link = u'<a href="{url}">{name}</a>'.format(
            url=candidate_path,
            name=candidate_name,
        )
        photo_review_url = self.request.build_absolute_uri(
            self.queued_image.get_absolute_url()
        )
        site_name = Site.objects.get_current().name
        def flash(level, message):
            messages.add_message(
                self.request,
                level,
                message,
                extra_tags='safe photo-review'
            )
        if decision == 'approved':
            # Crop the image...
            crop_fields = ('x_min', 'y_min', 'x_max', 'y_max')
            self.crop_and_upload_image_to_popit(
                self.queued_image.image.path,
                [form.cleaned_data[e] for e in crop_fields],
                form.cleaned_data['moderator_why_allowed'],
                form.cleaned_data['make_primary'],
            )
            self.queued_image.decision = 'approved'
            for i, field in enumerate(crop_fields):
                setattr(
                    self.queued_image,
                    'crop_' + field,
                    form.cleaned_data[field]
                )
            self.queued_image.save()
            update_message = _(u'Approved a photo upload from '
                u'{uploading_user} who provided the message: '
                u'"{message}"').format(
                uploading_user=self.queued_image.user.username,
                message=self.queued_image.justification_for_use,
            )
            change_metadata = get_change_metadata(
                self.request,
                update_message
            )
            person.extra.record_version(change_metadata)
            person.save()
            LoggedAction.objects.create(
                user=self.request.user,
                action_type='photo-approve',
                ip_address=get_client_ip(self.request),
                popit_person_new_version=change_metadata['version_id'],
                person=person,
                source=update_message,
            )
            candidate_full_url = person.extra.get_absolute_url(self.request)
            self.send_mail(
                _('{site_name} image upload approved').format(
                    site_name=site_name
                ),
                render_to_string(
                    'moderation_queue/photo_approved_email.txt',
                    {
                        'site_name': site_name,
                        'candidate_page_url': candidate_full_url,
                        'intro': _(
                            "Thank-you for submitting a photo to "
                            "{site_name}; that's been uploaded now for "
                            "the candidate page here:"
                        ).format(site_name=site_name),
                        'signoff': _(
                            "Many thanks from the {site_name} volunteers"
                        ).format(site_name=site_name),
                    }
                ),
            )
            flash(
                messages.SUCCESS,
                _(u'You approved a photo upload for %s') % candidate_link
            )
        elif decision == 'rejected':
            self.queued_image.decision = 'rejected'
            self.queued_image.save()
            update_message = _(u'Rejected a photo upload from '
                u'{uploading_user}').format(
                uploading_user=self.queued_image.user.username,
            )
            LoggedAction.objects.create(
                user=self.request.user,
                action_type='photo-reject',
                ip_address=get_client_ip(self.request),
                popit_person_new_version='',
                person=person,
                source=update_message,
            )
            retry_upload_link = self.request.build_absolute_uri(
                reverse(
                    'photo-upload',
                    kwargs={'popit_person_id': self.queued_image.person.id}
                )
            )
            self.send_mail(
                _('{site_name} image moderation results').format(
                    site_name=Site.objects.get_current().name
                ),
                render_to_string(
                    'moderation_queue/photo_rejected_email.txt',
                    {
                        'reason': form.cleaned_data['rejection_reason'],
                        'retry_upload_link': retry_upload_link,
                        'photo_review_url': photo_review_url,
                        'intro': _(
                            "Thank-you for uploading a photo of "
                            "{candidate_name} to {site_name}, but "
                            "unfortunately we can't use that image because:"
                        ).format(
                            candidate_name=candidate_name,
                            site_name=site_name
                        ),
                        'possible_actions': _(
                            'You can just reply to this email if you want to '
                            'discuss that further, or you can try uploading a '
                            'photo with a different reason or justification '
                            'for its use using this link:'
                        ),
                        'signoff': _(
                            "Many thanks from the {site_name} volunteers"
                        ).format(site_name=site_name),
                    },
                ),
                email_support_too=True,
            )
            flash(
                messages.INFO,
                _(u'You rejected a photo upload for %s') % candidate_link
            )
        elif decision == 'undecided':
            # If it's left as undecided, just redirect back to the
            # photo review queue...
            flash(
                messages.INFO,
                _(u'You left a photo upload for {0} in the queue').format(
                    candidate_link
                )
            )
        elif decision == 'ignore':
            self.queued_image.decision = 'ignore'
            self.queued_image.save()
            update_message = _(u'Ignored a photo upload from '
                u'{uploading_user} (This usually means it was a duplicate)').format(
                uploading_user=self.queued_image.user.username)
            LoggedAction.objects.create(
                user=self.request.user,
                action_type='photo-ignore',
                ip_address=get_client_ip(self.request),
                popit_person_new_version='',
                person=person,
                source=update_message,
            )
            flash(
                messages.INFO,
                _(u'You indicated a photo upload for {0} should be ignored').format(
                    candidate_link
                )
            )
        else:
            raise Exception("BUG: unexpected decision {0}".format(decision))
        return HttpResponseRedirect(reverse('photo-review-list'))

    def form_invalid(self, form):
        return self.render_to_response(
            self.get_context_data(
                queued_image_id=self.queued_image.id,
                form=form
            )
        )

    def post(self, request, *args, **kwargs):
        self.queued_image = QueuedImage.objects.get(
            pk=kwargs['queued_image_id']
        )
        form = PhotoReviewForm(data=self.request.POST)
        if form.is_valid():
            return self.form_valid(form)
        else:
            return self.form_invalid(form)
