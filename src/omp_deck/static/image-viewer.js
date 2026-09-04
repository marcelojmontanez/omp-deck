const imageViewer = document.getElementById('image-viewer');
const imageViewerImage = document.getElementById('image-viewer-image');
let imageViewerTrigger = null;

function closeImageViewer() {
  imageViewer.close();
  imageViewerImage.removeAttribute('src');
  imageViewerTrigger?.focus();
  imageViewerTrigger = null;
}

document.addEventListener('click', event => {
  const trigger = event.target.closest('[data-view-image]');
  if (!trigger) return;
  event.preventDefault();
  imageViewerTrigger = trigger;
  imageViewerImage.src = trigger.dataset.viewImage;
  imageViewer.showModal();
});

imageViewer.querySelector('.image-viewer-close').addEventListener('click', closeImageViewer);
imageViewer.addEventListener('click', event => {
  if (event.target === imageViewer) closeImageViewer();
});
imageViewer.addEventListener('cancel', event => {
  event.preventDefault();
  closeImageViewer();
});
