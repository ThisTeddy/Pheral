"use strict";

/*
|--------------------------------------------------------------------------
| PHERAL GLOBAL JAVASCRIPT
|--------------------------------------------------------------------------
*/

/*
|--------------------------------------------------------------------------
| CSRF
|--------------------------------------------------------------------------
*/

function getCSRFToken() {
    const cookie = document.cookie
        .split("; ")
        .find(row => row.startsWith("csrftoken="));

    return cookie ? decodeURIComponent(cookie.split("=")[1]) : "";
}


/*
|--------------------------------------------------------------------------
| Pheral fetch helper
|--------------------------------------------------------------------------
*/

async function pheralFetch(url, options = {}) {
    const defaultOptions = {
        headers: {
            "X-CSRFToken": getCSRFToken(),
            "X-Requested-With": "XMLHttpRequest",
        },
    };

    const response = await fetch(url, {
        ...defaultOptions,
        ...options,
        headers: {
            ...defaultOptions.headers,
            ...(options.headers || {}),
        },
    });

    return response;
}


/*
|--------------------------------------------------------------------------
| POST helper
|--------------------------------------------------------------------------
*/

async function pheralPost(url, data = {}) {
    return pheralFetch(url, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify(data),
    });
}


/*
|--------------------------------------------------------------------------
| PWA SERVICE WORKER
|--------------------------------------------------------------------------
*/

if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
        const serviceWorkerUrl = "/service-worker.js";

        navigator.serviceWorker
            .register(serviceWorkerUrl)
            .then(() => {
                console.log("Pheral service worker registered.");
            })
            .catch(error => {
                console.error(
                    "Pheral service worker registration failed:",
                    error
                );
            });
    });
}


/*
|--------------------------------------------------------------------------
| Prevent accidental double-submit
|--------------------------------------------------------------------------
*/

document.addEventListener("submit", event => {
    const form = event.target;

    if (!form.matches("[data-prevent-double-submit]")) {
        return;
    }

    const submitButton = form.querySelector(
        'button[type="submit"], input[type="submit"]'
    );

    if (!submitButton) {
        return;
    }

    submitButton.disabled = true;
    submitButton.dataset.originalText = submitButton.innerText;
    submitButton.innerText = "Please wait...";
});


/*
|--------------------------------------------------------------------------
| Global console marker
|--------------------------------------------------------------------------
*/

console.log("Pheral frontend initialized.");