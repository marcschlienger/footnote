const API_URL = 'http://localhost:8000';

let currentJobId = null;
let pollInterval = null;
let pushSubscription = null;

// DOM Elements
const form = document.getElementById('research-form');
const questionInput = document.getElementById('question');
const submitBtn = document.getElementById('submit-btn');
const btnText = submitBtn.querySelector('.btn-text');
const btnLoading = submitBtn.querySelector('.btn-loading');
const notificationBanner = document.getElementById('notification-banner');
const enableNotificationsBtn = document.getElementById('enable-notifications');
const statusSection = document.getElementById('status-section');
const statusBadge = document.getElementById('status-badge');
const jobIdSpan = document.getElementById('job-id');
const statusQuestion = document.getElementById('status-question');
const statusProgress = document.getElementById('status-progress');
const statusResult = document.getElementById('status-result');
const statusError = document.getElementById('status-error');

// Initialize app
document.addEventListener('DOMContentLoaded', async () => {
    await registerServiceWorker();
    checkNotificationPermission();
});

// Register service worker
async function registerServiceWorker() {
    if ('serviceWorker' in navigator) {
        try {
            const registration = await navigator.serviceWorker.register('service-worker.js');
            console.log('Service Worker registered:', registration.scope);

            // Get push subscription if notifications are enabled
            if (Notification.permission === 'granted') {
                await subscribeToPush(registration);
            }
        } catch (error) {
            console.error('Service Worker registration failed:', error);
        }
    }
}

// Check notification permission and show banner if needed
function checkNotificationPermission() {
    if (!('Notification' in window)) {
        console.log('Notifications not supported');
        return;
    }

    if (Notification.permission === 'default') {
        notificationBanner.hidden = false;
    } else if (Notification.permission === 'denied') {
        notificationBanner.hidden = true;
    } else {
        notificationBanner.hidden = true;
    }
}

// Enable notifications button click
enableNotificationsBtn.addEventListener('click', async () => {
    const permission = await Notification.requestPermission();

    if (permission === 'granted') {
        notificationBanner.hidden = true;
        const registration = await navigator.serviceWorker.ready;
        await subscribeToPush(registration);
    }
});

// Subscribe to push notifications
async function subscribeToPush(registration) {
    try {
        // Get VAPID public key from server
        const response = await fetch(`${API_URL}/vapid-public-key`);
        if (!response.ok) {
            console.error('Failed to get VAPID key');
            return;
        }
        const { publicKey } = await response.json();

        // Convert base64 to Uint8Array
        const vapidPublicKey = urlBase64ToUint8Array(publicKey);

        // Subscribe
        pushSubscription = await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: vapidPublicKey,
        });

        console.log('Push subscription:', pushSubscription);
    } catch (error) {
        console.error('Failed to subscribe to push:', error);
    }
}

// Convert base64 to Uint8Array for VAPID key
function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding)
        .replace(/-/g, '+')
        .replace(/_/g, '/');

    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);

    for (let i = 0; i < rawData.length; ++i) {
        outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
}

// Form submission
form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const question = questionInput.value.trim();
    if (!question) return;

    // Disable form
    setLoading(true);

    try {
        // Prepare request body
        const body = { question };

        if (pushSubscription) {
            body.subscription = {
                endpoint: pushSubscription.endpoint,
                keys: {
                    p256dh: btoa(String.fromCharCode(...new Uint8Array(pushSubscription.getKey('p256dh')))),
                    auth: btoa(String.fromCharCode(...new Uint8Array(pushSubscription.getKey('auth')))),
                },
            };
        }

        // Submit research request
        const response = await fetch(`${API_URL}/research`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();
        currentJobId = data.job_id;

        // Show status section
        showStatus(question, 'pending', 'Job created');

        // Start polling for status
        startPolling();

    } catch (error) {
        console.error('Failed to submit research:', error);
        alert(`Failed to submit research: ${error.message}`);
    } finally {
        setLoading(false);
    }
});

// Set loading state
function setLoading(loading) {
    submitBtn.disabled = loading;
    questionInput.disabled = loading;
    btnText.hidden = loading;
    btnLoading.hidden = !loading;
}

// Show status section
function showStatus(question, status, progress, result = null, error = null) {
    statusSection.hidden = false;
    jobIdSpan.textContent = currentJobId ? `ID: ${currentJobId.slice(0, 8)}...` : '';
    statusQuestion.textContent = question;
    statusProgress.textContent = progress || '';

    // Update badge
    statusBadge.textContent = status;
    statusBadge.className = `badge ${status}`;

    // Show/hide result link
    if (result) {
        statusResult.href = result;
        statusResult.hidden = false;
    } else {
        statusResult.hidden = true;
    }

    // Show/hide error
    if (error) {
        statusError.textContent = error;
        statusError.hidden = false;
    } else {
        statusError.hidden = true;
    }
}

// Start polling for job status
function startPolling() {
    if (pollInterval) {
        clearInterval(pollInterval);
    }

    pollInterval = setInterval(async () => {
        if (!currentJobId) {
            clearInterval(pollInterval);
            return;
        }

        try {
            const response = await fetch(`${API_URL}/research/${currentJobId}`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const data = await response.json();
            showStatus(data.question, data.status, data.progress, data.result, data.error);

            // Stop polling if job is complete or failed
            if (data.status === 'completed' || data.status === 'failed') {
                clearInterval(pollInterval);
                pollInterval = null;
            }
        } catch (error) {
            console.error('Failed to fetch status:', error);
        }
    }, 2000); // Poll every 2 seconds
}
