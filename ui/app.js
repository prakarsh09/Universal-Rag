// Global Configuration & State
const loc = window.location;
let host = loc.host || 'localhost:8000';

// If the UI is being served from port 3006, redirect API/WS requests to the backend at port 5005
if (loc.port === '3006') {
    host = `${loc.hostname}:5005`;
}

const protocol = loc.protocol === 'https:' ? 'https:' : 'http:';
const wsProtocol = loc.protocol === 'https:' ? 'wss:' : 'ws:';

const API_STATUS_URL = `${protocol}//${host}/api/status`;
const WS_CHAT_URL = `${wsProtocol}//${host}/api/chat`;
const API_DOCS_UPLOAD_URL = `${protocol}//${host}/api/documents/upload`;
const API_DOCS_QUEUE_URL = `${protocol}//${host}/api/documents/queue`;
const API_IMAGES_LIST_URL = `${protocol}//${host}/api/images`;
const API_DOCS_LIST_URL = `${protocol}//${host}/api/documents`;

let chatHistory = [];
let ws = null;
let currentAssistantBubble = null;
let currentMediaContainer = null;
let currentTextContainer = null;
let currentStatusLog = null;
let accumulatedResponseText = '';

// DOM Elements
let chatMessages, chatForm, userInput, btnSend, btnClearChat, welcomeMessage;
let sourcesContainer, sourcesList, sourceCount, btnToggleSources;

// Mobile UI Elements
let sidebar, sidebarOverlay, btnMenuToggle, btnSidebarClose;

// Status indicators
let indicatorApi, labelApi, indicatorNeo4j, labelNeo4j, activeModelLabel;

// Tab & View Elements
let tabBtnChat, tabBtnDocs, headerTabChat, headerTabDocs;
let viewChat, viewDocs, mainHeaderTitle, mainHeaderSubtitle;
let navQueueBadge, headerQueueDot;

// Document Management State & Elements
let currentSelectedFile = null;
let queuePollingTimer = null;
let lastWorkerWasBusy = false;
let indexedDocsCache = [];
let indexedImagesCache = [];

// Set Markdown configuration (disable mangling/header IDs to prevent errors in newer versions)
if (window.marked) {
    window.marked.setOptions({
        breaks: true,
        gfm: true
    });
}

// -------------------------------------------------------------
// Initialize App
// -------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    // Resolve DOM Elements
    chatMessages = document.getElementById('chat-messages');
    chatForm = document.getElementById('chat-form');
    userInput = document.getElementById('user-input');
    btnSend = document.getElementById('btn-send');
    btnClearChat = document.getElementById('btn-clear-chat');
    welcomeMessage = document.getElementById('welcome-message');
    sourcesContainer = document.getElementById('sources-container');
    sourcesList = document.getElementById('sources-list');
    sourceCount = document.getElementById('source-count');
    btnToggleSources = document.getElementById('btn-toggle-sources');

    // Resolve Mobile UI Elements
    sidebar = document.querySelector('.sidebar');
    sidebarOverlay = document.getElementById('sidebar-overlay');
    btnMenuToggle = document.getElementById('btn-menu-toggle');
    btnSidebarClose = document.getElementById('btn-sidebar-close');

    // Resolve Status indicators
    indicatorApi = document.querySelector('#status-api .status-indicator');
    labelApi = document.querySelector('#status-api .status-label');
    indicatorNeo4j = document.querySelector('#status-neo4j .status-indicator');
    labelNeo4j = document.querySelector('#status-neo4j .status-label');
    activeModelLabel = document.getElementById('active-model');

    console.log('[App] Resolving DOM elements...');

    loadChatHistory();
    checkSystemStatus();
    connectWebSocket();
    initTabs();
    initDocumentsTab();
    
    // Poll system status every 5 seconds
    setInterval(checkSystemStatus, 5000);
    
    // Auto-grow input textarea
    if (userInput) {
        userInput.addEventListener('input', autoGrowInput);
        
        // Handle Enter to send
        userInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                chatForm.dispatchEvent(new Event('submit'));
            }
        });
    }

    // Handle form submit
    if (chatForm) {
        chatForm.addEventListener('submit', (e) => {
            e.preventDefault();
            const message = userInput.value.trim();
            if (!message) return;
            
            sendQuery(message);
            userInput.value = '';
            userInput.style.height = 'auto'; // Reset height
        });
    }

    // Mobile Sidebar helper functions
    const closeSidebar = () => {
        console.log('[Sidebar] Closing sidebar...');
        if (sidebar && sidebarOverlay) {
            sidebar.classList.remove('active');
            sidebarOverlay.classList.remove('active');
        }
    };

    // Mobile Sidebar Event Listeners
    if (btnMenuToggle && sidebar && sidebarOverlay) {
        btnMenuToggle.addEventListener('click', () => {
            console.log('[Sidebar] Opening sidebar...');
            sidebar.classList.add('active');
            sidebarOverlay.classList.add('active');
        });
    }

    if (btnSidebarClose) {
        btnSidebarClose.addEventListener('click', closeSidebar);
    }

    if (sidebarOverlay) {
        sidebarOverlay.addEventListener('click', closeSidebar);
    }

    // Handle suggestion clicks
    document.addEventListener('click', (e) => {
        const card = e.target.closest('.suggestion-card, .suggest-btn');
        if (card) {
            const query = card.getAttribute('data-query');
            if (query) {
                sendQuery(query);
                // Close sidebar on mobile after choosing a suggestion
                if (window.innerWidth <= 768) {
                    closeSidebar();
                }
            }
        }
    });

    // Toggle sources drawer
    if (btnToggleSources) {
        btnToggleSources.addEventListener('click', () => {
            const isCollapsed = sourcesList.style.display === 'none';
            sourcesList.style.display = isCollapsed ? 'grid' : 'none';
            btnToggleSources.querySelector('i').className = isCollapsed 
                ? 'fa-solid fa-chevron-down' 
                : 'fa-solid fa-chevron-up';
        });
    }

    // Clear conversation
    if (btnClearChat) {
        btnClearChat.addEventListener('click', clearConversation);
    }

    // Initialize Liquid Background Animation
    initLiquidBg();
});

// -------------------------------------------------------------
// System Health Monitoring
// -------------------------------------------------------------
async function checkSystemStatus() {
    try {
        const res = await fetch(API_STATUS_URL);
        if (!res.ok) throw new Error('API return non-200');
        const data = await res.json();
        
        // Update API Label
        indicatorApi.className = 'status-indicator online';
        labelApi.textContent = 'Backend API: Online';
        
        // Update Neo4j Label
        if (data.neo4j_connected) {
            indicatorNeo4j.className = 'status-indicator online';
            labelNeo4j.textContent = `Neo4j DB: Connected (${data.neo4j_chunks} chunks)`;
        } else {
            indicatorNeo4j.className = 'status-indicator offline';
            labelNeo4j.textContent = 'Neo4j DB: Offline';
        }
        
        // Update Model Badge
        activeModelLabel.textContent = data.llm_model;
        
    } catch (err) {
        indicatorApi.className = 'status-indicator offline';
        labelApi.textContent = 'Backend API: Offline';
        indicatorNeo4j.className = 'status-indicator offline';
        labelNeo4j.textContent = 'Neo4j DB: Offline';
    }
}

// -------------------------------------------------------------
// WebSocket Connection Manager
// -------------------------------------------------------------
function connectWebSocket() {
    if (ws && ws.readyState === WebSocket.OPEN) return;

    console.log(`[WebSocket] Connecting to: ${WS_CHAT_URL}`);
    try {
        ws = new WebSocket(WS_CHAT_URL);

        ws.onopen = () => {
            console.log('[WebSocket] Connection established.');
        };

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            handleServerMessage(data);
        };

        ws.onerror = (error) => {
            console.error('[WebSocket] Error:', error);
        };

        ws.onclose = (event) => {
            console.log('[WebSocket] Connection closed. Reconnecting in 3s...', event.reason);
            setTimeout(connectWebSocket, 3000);
        };
    } catch (error) {
        console.warn('[WebSocket] Connection failed to initialize (Backend might be offline):', error);
        // Retry connection in 5 seconds
        setTimeout(connectWebSocket, 5000);
    }
}

// -------------------------------------------------------------
// Chat Operations
// -------------------------------------------------------------

function sendQuery(message) {
    if (ws.readyState !== WebSocket.OPEN) {
        appendMessage('system', 'Unable to send query. Reconnecting to server, please wait...');
        connectWebSocket();
        return;
    }

    // Hide welcome card if visible
    if (welcomeMessage) {
        welcomeMessage.style.display = 'none';
    }

    // Clear sources drawer from previous turn
    sourcesContainer.style.display = 'none';
    sourcesList.innerHTML = '';

    // Append user message
    appendMessage('user', message);

    // Push user turn to history
    chatHistory.push({ role: 'user', content: message });
    saveChatHistory();

    // Append assistant typing skeleton
    createAssistantBubble();

    // Send payload over WebSocket
    const payload = {
        message: message,
        history: chatHistory
    };
    ws.send(JSON.stringify(payload));
    disableInput(true);
}

function handleServerMessage(data) {
    switch (data.type) {
        case 'status':
            // Update typing indicator text log
            if (currentStatusLog) {
                currentStatusLog.innerHTML = `<i class="fa-solid fa-spinner"></i> ${data.content}`;
            }
            break;
            
        case 'sources':
            // Render retrieved sources
            renderSources(data.content);
            break;

        case 'image_results':
            renderImageResults(data.content);
            break;
            
        case 'token':
            // Remove status/typing indicators on first token
            if (currentStatusLog) {
                currentStatusLog.remove();
                currentStatusLog = null;
            }
            if (currentAssistantBubble) {
                const typingInd = currentAssistantBubble.querySelector('.typing-indicator');
                if (typingInd) typingInd.remove();
            }

            // Stream token
            accumulatedResponseText += data.content;
            
            // Render markdown live into text container so images are never overwritten
            const targetTextElem = currentTextContainer || currentAssistantBubble;
            if (targetTextElem) {
                if (window.marked) {
                    targetTextElem.innerHTML = window.marked.parse(accumulatedResponseText);
                } else {
                    targetTextElem.textContent = accumulatedResponseText;
                }
            }
            scrollToBottom();
            break;
            
        case 'done':
            // Save complete assistant turn to history
            chatHistory.push({ role: 'assistant', content: accumulatedResponseText });
            saveChatHistory();
            
            // Clean up state
            resetTurnState();
            disableInput(false);
            break;
            
        case 'error':
            if (currentStatusLog) currentStatusLog.remove();
            const ind = currentAssistantBubble.querySelector('.typing-indicator');
            if (ind) ind.remove();
            
            currentAssistantBubble.innerHTML = `<div style="color: var(--danger);"><i class="fa-solid fa-triangle-exclamation"></i> ${data.content}</div>`;
            resetTurnState();
            disableInput(false);
            break;
    }
}

function resetTurnState() {
    currentAssistantBubble = null;
    currentMediaContainer = null;
    currentTextContainer = null;
    currentStatusLog = null;
    accumulatedResponseText = '';
}

function disableInput(disabled) {
    userInput.disabled = disabled;
    btnSend.disabled = disabled;
    if (!disabled) {
        userInput.focus();
    }
}

// -------------------------------------------------------------
// Message Rendering
// -------------------------------------------------------------
function appendMessage(role, text) {
    const msgElement = document.createElement('div');
    msgElement.className = `message ${role}`;

    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.innerHTML = role === 'user' ? '<i class="fa-solid fa-user"></i>' : '<i class="fa-solid fa-robot"></i>';

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    
    if (role === 'user') {
        bubble.textContent = text;
    } else {
        bubble.innerHTML = window.marked ? window.marked.parse(text) : text;
    }

    msgElement.appendChild(avatar);
    msgElement.appendChild(bubble);
    chatMessages.appendChild(msgElement);
    scrollToBottom();
}

function createAssistantBubble() {
    const msgElement = document.createElement('div');
    msgElement.className = 'message assistant';

    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.innerHTML = '<i class="fa-solid fa-robot"></i>';

    const bubble = document.createElement('div');
    bubble.className = 'bubble';

    // Status Log for search activity
    currentStatusLog = document.createElement('div');
    currentStatusLog.className = 'status-log';
    currentStatusLog.innerHTML = '<i class="fa-solid fa-spinner"></i> Initiating hybrid search...';
    
    // Dedicated sub-containers: media stays intact above streamed text
    const mediaContainer = document.createElement('div');
    mediaContainer.className = 'bubble-media-container';

    const textContainer = document.createElement('div');
    textContainer.className = 'bubble-text-container';

    // Typing indicator
    const typingIndicator = document.createElement('div');
    typingIndicator.className = 'typing-indicator';
    typingIndicator.innerHTML = '<span></span><span></span><span></span>';

    bubble.appendChild(currentStatusLog);
    bubble.appendChild(mediaContainer);
    bubble.appendChild(textContainer);
    bubble.appendChild(typingIndicator);
    
    msgElement.appendChild(avatar);
    msgElement.appendChild(bubble);
    chatMessages.appendChild(msgElement);
    
    currentAssistantBubble = bubble;
    currentMediaContainer = mediaContainer;
    currentTextContainer = textContainer;
    scrollToBottom();
}

function renderSources(sources) {
    if (!sources || sources.length === 0) return;
    
    sourceCount.textContent = sources.length;
    sourcesContainer.style.display = 'block';
    sourcesList.innerHTML = '';
    sourcesList.style.display = 'grid'; // ensure expanded
    btnToggleSources.querySelector('i').className = 'fa-solid fa-chevron-down';

    sources.forEach(src => {
        const item = document.createElement('div');
        item.className = 'source-item';
        
        // Clean up score format
        const scorePct = (src.score * 100).toFixed(0);
        
        item.innerHTML = `
            <div class="source-doc" title="${src.document_name}">
                <i class="fa-solid fa-file-pdf" style="color: var(--danger);"></i> ${src.document_name}
            </div>
            <div class="source-meta">
                <span>Category: <strong>${src.category || "General"}</strong></span>
                <span style="color: var(--accent);">Match: ${scorePct}%</span>
            </div>
            <div class="source-path" title="${src.header_path}">
                Path: ${src.header_path}
            </div>
        `;
        sourcesList.appendChild(item);
    });
}

// -------------------------------------------------------------
// Local Storage Persistence
// -------------------------------------------------------------
function saveChatHistory() {
    localStorage.setItem('generic_rag_history', JSON.stringify(chatHistory));
}

function loadChatHistory() {
    const saved = localStorage.getItem('generic_rag_history');
    if (saved) {
        chatHistory = JSON.parse(saved);
        if (chatHistory.length > 0) {
            if (welcomeMessage) welcomeMessage.style.display = 'none';
            chatHistory.forEach(turn => {
                appendMessage(turn.role, turn.content);
            });
        }
    }
}

function clearConversation() {
    chatHistory = [];
    localStorage.removeItem('generic_rag_history');
    
    // Clear DOM
    const welcome = welcomeMessage.cloneNode(true);
    welcome.style.display = 'block';
    
    chatMessages.innerHTML = '';
    chatMessages.appendChild(welcome);
    
    sourcesContainer.style.display = 'none';
    sourcesList.innerHTML = '';
    
    resetTurnState();
    disableInput(false);
}

// -------------------------------------------------------------
// Helper Utilities
// -------------------------------------------------------------
function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function autoGrowInput() {
    userInput.style.height = 'auto';
    userInput.style.height = userInput.scrollHeight + 'px';
}

// -------------------------------------------------------------
// Liquid Background Canvas Animation
// -------------------------------------------------------------
class LiquidBlob {
    constructor(width, height, color) {
        this.x = Math.random() * width;
        this.y = Math.random() * height;
        // Slow shifting velocities
        this.vx = (Math.random() - 0.5) * 0.6;
        this.vy = (Math.random() - 0.5) * 0.6;
        this.radius = Math.random() * 150 + 150; // large soft blobs
        this.color = color;
    }

    update(width, height) {
        this.x += this.vx;
        this.y += this.vy;

        // Bounce boundaries and correct position to avoid getting stuck
        if (this.x < 0) {
            this.x = 0;
            this.vx = Math.abs(this.vx);
        } else if (this.x > width) {
            this.x = width;
            this.vx = -Math.abs(this.vx);
        }

        if (this.y < 0) {
            this.y = 0;
            this.vy = Math.abs(this.vy);
        } else if (this.y > height) {
            this.y = height;
            this.vy = -Math.abs(this.vy);
        }
    }

    draw(ctx) {
        ctx.beginPath();
        const grad = ctx.createRadialGradient(this.x, this.y, 0, this.x, this.y, this.radius);
        grad.addColorStop(0, this.color);
        grad.addColorStop(1, 'rgba(0, 0, 0, 0)');
        ctx.fillStyle = grad;
        ctx.arc(this.x, this.y, this.radius, 0, Math.PI * 2);
        ctx.fill();
    }
}

function initLiquidBg() {
    console.log('[LiquidBG] Initializing liquid background...');
    const canvas = document.getElementById('liquid-bg-canvas');
    if (!canvas) {
        console.warn('[LiquidBG] Canvas element not found!');
        return;
    }
    const ctx = canvas.getContext('2d');
    if (!ctx) {
        console.warn('[LiquidBG] Could not retrieve 2D context from canvas!');
        return;
    }

    // Run canvas at a lower internal resolution for rendering performance (stretched by CSS + blur filter)
    const scaleFactor = 0.5;
    
    // Safely get width and height with window fallbacks
    let offsetW = canvas.offsetWidth;
    let offsetH = canvas.offsetHeight;
    if (!offsetW || offsetW === 0) offsetW = window.innerWidth || 1024;
    if (!offsetH || offsetH === 0) offsetH = window.innerHeight || 768;
    
    let width = canvas.width = offsetW * scaleFactor;
    let height = canvas.height = offsetH * scaleFactor;
    console.log(`[LiquidBG] Canvas resolution set to ${width}x${height}`);

    // Vibrant, higher-opacity colors for beautiful blending under the CSS blur filter
    const colors = [
        'rgba(99, 102, 241, 0.75)',  // --primary (Indigo)
        'rgba(6, 182, 212, 0.7)',    // --accent (Cyan)
        'rgba(139, 92, 246, 0.65)',  // Deep Violet
        'rgba(30, 58, 138, 0.7)'     // Deep Blue
    ];

    const blobs = colors.map(c => new LiquidBlob(width, height, c));

    function resize() {
        if (!canvas) return;
        let w = canvas.offsetWidth;
        let h = canvas.offsetHeight;
        if (!w || w === 0) w = window.innerWidth || 1024;
        if (!h || h === 0) h = window.innerHeight || 768;
        width = canvas.width = w * scaleFactor;
        height = canvas.height = h * scaleFactor;
    }

    window.addEventListener('resize', resize);

    function animate() {
        if (!canvas) return;
        ctx.clearRect(0, 0, width, height);
        
        // Use default source-over compositing, which works reliably on transparent backgrounds
        ctx.globalCompositeOperation = 'source-over';

        blobs.forEach(blob => {
            blob.update(width, height);
            blob.draw(ctx);
        });

        requestAnimationFrame(animate);
    }

    animate();
    console.log('[LiquidBG] Animation loop running.');
}


// ==========================================================================
// Tabs & Documents Management Logic
// ==========================================================================

function initTabs() {
    tabBtnChat = document.getElementById('tab-btn-chat');
    tabBtnDocs = document.getElementById('tab-btn-docs');
    headerTabChat = document.getElementById('header-tab-chat');
    headerTabDocs = document.getElementById('header-tab-docs');
    viewChat = document.getElementById('view-chat');
    viewDocs = document.getElementById('view-docs');
    mainHeaderTitle = document.getElementById('main-header-title');
    mainHeaderSubtitle = document.getElementById('main-header-subtitle');
    navQueueBadge = document.getElementById('nav-queue-badge');
    headerQueueDot = document.getElementById('header-queue-dot');

    function switchTab(tabId) {
        if (tabId === 'chat') {
            if (tabBtnChat) tabBtnChat.classList.add('active');
            if (tabBtnDocs) tabBtnDocs.classList.remove('active');
            if (headerTabChat) headerTabChat.classList.add('active');
            if (headerTabDocs) headerTabDocs.classList.remove('active');

            if (viewChat) viewChat.style.display = 'flex';
            if (viewDocs) viewDocs.style.display = 'none';

            if (mainHeaderTitle) mainHeaderTitle.textContent = 'Universal Knowledge Graph Agent';
            if (mainHeaderSubtitle) mainHeaderSubtitle.textContent = 'Hybrid Vector + Full-Text Search with Graph-Based Context Expansion';
        } else if (tabId === 'docs') {
            if (tabBtnDocs) tabBtnDocs.classList.add('active');
            if (tabBtnChat) tabBtnChat.classList.remove('active');
            if (headerTabDocs) headerTabDocs.classList.add('active');
            if (headerTabChat) headerTabChat.classList.remove('active');

            if (viewDocs) viewDocs.style.display = 'flex';
            if (viewChat) viewChat.style.display = 'none';

            if (mainHeaderTitle) mainHeaderTitle.textContent = 'Knowledge Graph Documents';
            if (mainHeaderSubtitle) mainHeaderSubtitle.textContent = 'Upload & Ingest documents with background worker queue (Concurrency = 1)';

            // Fetch live status when switching to Docs tab
            fetchQueueStatus();
            fetchIndexedDocuments();
        }

        // Close sidebar on mobile after tab selection
        if (sidebar && sidebar.classList.contains('open')) {
            sidebar.classList.remove('open');
            if (sidebarOverlay) sidebarOverlay.classList.remove('active');
        }
    }

    if (tabBtnChat) tabBtnChat.addEventListener('click', () => switchTab('chat'));
    if (tabBtnDocs) tabBtnDocs.addEventListener('click', () => switchTab('docs'));
    if (headerTabChat) headerTabChat.addEventListener('click', () => switchTab('chat'));
    if (headerTabDocs) headerTabDocs.addEventListener('click', () => switchTab('docs'));

    const btnWelcomeDocs = document.getElementById('btn-welcome-docs');
    if (btnWelcomeDocs) {
        btnWelcomeDocs.addEventListener('click', () => switchTab('docs'));
    }

    // Support direct URL hash navigation (#documents or #chat)
    if (window.location.hash === '#documents' || window.location.hash === '#docs') {
        switchTab('docs');
    }

    window.addEventListener('hashchange', () => {
        if (window.location.hash === '#documents' || window.location.hash === '#docs') {
            switchTab('docs');
        } else {
            switchTab('chat');
        }
    });
}

function initDocumentsTab() {
    const fileDropzone = document.getElementById('file-dropzone');
    const fileInput = document.getElementById('file-input');
    const btnBrowseFile = document.getElementById('btn-browse-file');
    const selectedFilePreview = document.getElementById('selected-file-preview');
    const previewFilename = document.getElementById('preview-filename');
    const previewFilesize = document.getElementById('preview-filesize');
    const btnRemoveFile = document.getElementById('btn-remove-file');

    const categoryInput = document.getElementById('category-input');
    const categoryTags = document.querySelectorAll('.cat-tag');
    const uploadForm = document.getElementById('document-upload-form');
    const btnUploadSubmit = document.getElementById('btn-upload-submit');
    const uploadAlert = document.getElementById('upload-alert');

    const statTotalDocs = document.getElementById('stat-total-docs');
    const statTotalChunks = document.getElementById('stat-total-chunks');

    const workerIdleView = document.getElementById('worker-idle-view');
    const workerProcessingView = document.getElementById('worker-processing-view');
    const activeDocName = document.getElementById('active-doc-name');
    const activeDocCategory = document.getElementById('active-doc-category');
    const activeDocPercent = document.getElementById('active-doc-percent');
    const activeProgressBar = document.getElementById('active-progress-bar');
    const activeStatusMessage = document.getElementById('active-status-message');

    const stepConverting = document.getElementById('step-converting');
    const stepChunking = document.getElementById('step-chunking');
    const stepEmbedding = document.getElementById('step-embedding');
    const stepIndexing = document.getElementById('step-indexing');

    const queueCountDisplay = document.getElementById('queue-count-display');
    const queueList = document.getElementById('queue-list');

    const filterDocsInput = document.getElementById('filter-docs-input');
    const btnRefreshLibrary = document.getElementById('btn-refresh-library');
    const docsTableBody = document.getElementById('docs-table-body');

    // Category Tags selection
    categoryTags.forEach(tag => {
        tag.addEventListener('click', () => {
            categoryTags.forEach(t => t.classList.remove('active'));
            tag.classList.add('active');
            if (categoryInput) {
                categoryInput.value = tag.getAttribute('data-cat') || 'General';
            }
        });
    });

    // Helper: format file size
    function formatBytes(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    // Handle File Selection
    function selectFile(file) {
        if (!file) return;

        const allowedExts = ['.pdf', '.md', '.markdown', '.txt', '.jpeg', '.jpg', '.png', '.webp'];
        const ext = '.' + file.name.split('.').pop().toLowerCase();
        if (!allowedExts.includes(ext)) {
            showUploadAlert(`Unsupported file type '${ext}'. Please choose a PDF, Markdown (.md), Text (.txt), or Image (.jpg, .jpeg, .png) file.`, 'error');
            return;
        }

        currentSelectedFile = file;
        if (previewFilename) previewFilename.textContent = file.name;
    // Handle image preview thumbnail
    const thumbImg = document.getElementById('preview-image-thumb');
    const fileIcon = document.getElementById('preview-file-icon');
    if (thumbImg && fileIcon) {
        if (file.type.startsWith('image/') || /\.(jpeg|jpg|png|webp)$/i.test(file.name)) {
            const reader = new FileReader();
            reader.onload = (e) => {
                thumbImg.src = e.target.result;
                thumbImg.style.display = 'block';
                fileIcon.style.display = 'none';
            };
            reader.readAsDataURL(file);
        } else {
            thumbImg.style.display = 'none';
            fileIcon.style.display = 'block';
        }
    }
        if (previewFilesize) previewFilesize.textContent = formatBytes(file.size);

        const dropContent = fileDropzone.querySelector('.dropzone-content');
        if (dropContent) dropContent.style.display = 'none';
        if (selectedFilePreview) selectedFilePreview.style.display = 'flex';
        if (btnUploadSubmit) btnUploadSubmit.disabled = false;
        if (uploadAlert) uploadAlert.style.display = 'none';
    }

    function clearFile() {
        currentSelectedFile = null;
        if (fileInput) fileInput.value = '';
        const dropContent = fileDropzone.querySelector('.dropzone-content');
        if (dropContent) dropContent.style.display = 'block';
        if (selectedFilePreview) selectedFilePreview.style.display = 'none';
        if (btnUploadSubmit) btnUploadSubmit.disabled = true;
    }

    // Dropzone Event Listeners
    if (fileDropzone) {
        ['dragenter', 'dragover'].forEach(eventName => {
            fileDropzone.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
                fileDropzone.classList.add('dragover');
            });
        });

        ['dragleave', 'drop'].forEach(eventName => {
            fileDropzone.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
                fileDropzone.classList.remove('dragover');
            });
        });

        fileDropzone.addEventListener('drop', (e) => {
            if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                selectFile(e.dataTransfer.files[0]);
            }
        });

        fileDropzone.addEventListener('click', (e) => {
            if (e.target.closest('#btn-remove-file')) return;
            if (fileInput) fileInput.click();
        });
    }

    if (btnBrowseFile) {
        btnBrowseFile.addEventListener('click', (e) => {
            e.stopPropagation();
            if (fileInput) fileInput.click();
        });
    }

    if (fileInput) {
        fileInput.addEventListener('change', (e) => {
            if (e.target.files && e.target.files.length > 0) {
                selectFile(e.target.files[0]);
            }
        });
    }

    if (btnRemoveFile) {
        btnRemoveFile.addEventListener('click', (e) => {
            e.stopPropagation();
            clearFile();
        });
    }

    function showUploadAlert(msg, type = 'success') {
        if (!uploadAlert) return;
        uploadAlert.textContent = msg;
        uploadAlert.className = `upload-alert ${type}`;
        uploadAlert.style.display = 'block';
    }

    // Upload Form Submission
    if (uploadForm) {
        uploadForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            if (!currentSelectedFile) return;

            const category = categoryInput ? (categoryInput.value.trim() || 'General') : 'General';
            const formData = new FormData();
            formData.append('file', currentSelectedFile);
            formData.append('category', category);

            btnUploadSubmit.disabled = true;
            btnUploadSubmit.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Enqueueing...';

            try {
                const res = await fetch(API_DOCS_UPLOAD_URL, {
                    method: 'POST',
                    body: formData
                });
                const data = await res.json();

                if (res.ok && data.success) {
                    showUploadAlert(`✓ Document '${data.job.document_name}' added to ingestion queue (Queue Position: #${data.job.queue_position}).`, 'success');
                    clearFile();
                    fetchQueueStatus();
                } else {
                    showUploadAlert(`Upload failed: ${data.detail || 'Unknown error'}`, 'error');
                }
            } catch (err) {
                console.error('[Upload] Error uploading file:', err);
                showUploadAlert(`Network error: ${err.message}`, 'error');
            } finally {
                btnUploadSubmit.disabled = currentSelectedFile === null;
                btnUploadSubmit.innerHTML = '<i class="fa-solid fa-plus-circle"></i> Add to Ingestion Queue';
            }
        });
    }

    // Queue Polling Function (Exposed to global window for tab switches)
    window.fetchQueueStatus = async function() {
        try {
            const res = await fetch(API_DOCS_QUEUE_URL);
            if (!res.ok) return;
            const data = await res.json();

            const isBusy = data.is_busy && data.active_job;
            const queueItems = data.queue || [];

            // Update compute device badge
            if (data.device_info) {
                const devPill = document.getElementById('worker-device-pill');
                const devName = document.getElementById('worker-device-name');
                if (devName && data.device_info.device_name) {
                    devName.textContent = data.device_info.cuda_available ? `CUDA: ${data.device_info.device_name}` : 'CPU Mode';
                }
                if (devPill) {
                    devPill.className = data.device_info.cuda_available ? 'device-pill cuda' : 'device-pill cpu';
                }
            }

            // Update badge in sidebar and header dot
            if (navQueueBadge) {
                if (queueItems.length > 0) {
                    navQueueBadge.textContent = queueItems.length;
                    navQueueBadge.style.display = 'inline-block';
                } else {
                    navQueueBadge.style.display = 'none';
                }
            }
            if (headerQueueDot) {
                headerQueueDot.style.display = isBusy ? 'inline-block' : 'none';
            }

            // Update Active Task UI
            if (isBusy) {
                const job = data.active_job;
                if (workerIdleView) workerIdleView.style.display = 'none';
                if (workerProcessingView) workerProcessingView.style.display = 'flex';

                if (activeDocName) activeDocName.textContent = job.original_filename || job.document_name;
                if (activeDocCategory) activeDocCategory.textContent = job.category || 'General';
                if (activeDocPercent) activeDocPercent.textContent = `${job.progress_percent || 0}%`;
                if (activeProgressBar) activeProgressBar.style.width = `${job.progress_percent || 0}%`;
                if (activeStatusMessage) activeStatusMessage.textContent = job.status_message || 'Processing...';

                // Update stage stepper
                const status = job.status;
                if (stepConverting) stepConverting.className = 'step-item' + (status === 'converting' ? ' active' : (['chunking', 'embedding', 'indexing', 'completed'].includes(status) ? ' done' : ''));
                if (stepChunking) stepChunking.className = 'step-item' + (status === 'chunking' ? ' active' : (['embedding', 'indexing', 'completed'].includes(status) ? ' done' : ''));
                if (stepEmbedding) stepEmbedding.className = 'step-item' + (status === 'embedding' ? ' active' : (['indexing', 'completed'].includes(status) ? ' done' : ''));
                if (stepIndexing) stepIndexing.className = 'step-item' + (status === 'indexing' ? ' active' : (status === 'completed' ? ' done' : ''));

                lastWorkerWasBusy = true;
            } else {
                if (workerIdleView) workerIdleView.style.display = 'block';
                if (workerProcessingView) workerProcessingView.style.display = 'none';

                // If worker was busy on the previous tick and is now idle, refresh indexed library
                if (lastWorkerWasBusy) {
                    lastWorkerWasBusy = false;
                    fetchIndexedDocuments();
                    checkSystemStatus();
                }
            }

            // Update Waiting Queue list
            if (queueCountDisplay) queueCountDisplay.textContent = queueItems.length;
            if (queueList) {
                if (queueItems.length === 0) {
                    queueList.innerHTML = '<div class="queue-empty">Queue is empty</div>';
                } else {
                    queueList.innerHTML = queueItems.map(item => `
                        <div class="queue-item">
                            <span class="queue-pos">#${item.queue_position}</span>
                            <span class="queue-item-name" title="${escapeHtml(item.original_filename)}">${escapeHtml(item.original_filename)}</span>
                            <span class="category-badge">${escapeHtml(item.category)}</span>
                        </div>
                    `).join('');
                }
            }

            // If worker is busy or there are queued items, poll again in 2 seconds
            if (isBusy || queueItems.length > 0) {
                clearTimeout(queuePollingTimer);
                queuePollingTimer = setTimeout(fetchQueueStatus, 2000);
            }
        } catch (err) {
            console.warn('[Queue] Polling error:', err);
        }
    };

    // Fetch Indexed Documents Library from Neo4j
    window.fetchIndexedDocuments = async function() {
        try {
            if (docsTableBody) {
                docsTableBody.innerHTML = '<tr><td colspan="5" class="table-loading"><i class="fa-solid fa-spinner fa-spin"></i> Querying Neo4j Knowledge Graph...</td></tr>';
            }

            const res = await fetch(API_DOCS_LIST_URL);
            if (!res.ok) throw new Error('Failed to fetch indexed documents');
            const data = await res.json();

            indexedDocsCache = data.documents || [];

            // Update Summary Stats
            if (statTotalDocs) statTotalDocs.textContent = indexedDocsCache.length;
            const totalChunks = indexedDocsCache.reduce((acc, d) => acc + (d.chunk_count || 0), 0);
            if (statTotalChunks) statTotalChunks.textContent = totalChunks;

            renderDocsTable(indexedDocsCache);
        } catch (err) {
            console.error('[Library] Error loading documents:', err);
            if (docsTableBody) {
                docsTableBody.innerHTML = `<tr><td colspan="5" class="table-empty" style="color: var(--danger);">Failed to load documents: ${err.message}</td></tr>`;
            }
        }
    };

    function renderDocsTable(docs) {
        if (!docsTableBody) return;

        if (!docs || docs.length === 0) {
            docsTableBody.innerHTML = `
                <tr>
                    <td colspan="5" class="table-empty">
                        <i class="fa-solid fa-folder-open" style="font-size: 1.5rem; margin-bottom: 8px; display: block; color: var(--text-dark);"></i>
                        No documents indexed in the Knowledge Graph yet. Upload a document above to get started!
                    </td>
                </tr>
            `;
            return;
        }

        docsTableBody.innerHTML = docs.map(doc => `
            <tr>
                <td>
                    <div class="table-doc-name">
                        <i class="fa-solid fa-file-lines table-doc-icon"></i>
                        <span>${escapeHtml(doc.name)}</span>
                    </div>
                </td>
                <td><span class="category-badge">${escapeHtml(doc.category)}</span></td>
                <td><strong>${doc.chunk_count}</strong> chunks</td>
                <td><span class="badge-indexed">Indexed</span></td>
                <td style="text-align: right;">
                    <button class="btn-delete-doc" data-docname="${escapeHtml(doc.name)}" title="Delete document and chunks from graph">
                        <i class="fa-solid fa-trash-can"></i> Delete
                    </button>
                </td>
            </tr>
        `).join('');

        // Wire up delete buttons
        docsTableBody.querySelectorAll('.btn-delete-doc').forEach(btn => {
            btn.addEventListener('click', async () => {
                const docName = btn.getAttribute('data-docname');
                if (!docName) return;

                if (!confirm(`Are you sure you want to delete "${docName}" and all its chunks from the Knowledge Graph?`)) {
                    return;
                }

                btn.disabled = true;
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';

                try {
                    const deleteUrl = `${API_DOCS_LIST_URL}/${encodeURIComponent(docName)}`;
                    const res = await fetch(deleteUrl, { method: 'DELETE' });
                    if (res.ok) {
                        fetchIndexedDocuments();
                        checkSystemStatus();
                    } else {
                        const errData = await res.json();
                        alert(`Failed to delete document: ${errData.detail || 'Unknown error'}`);
                        btn.disabled = false;
                        btn.innerHTML = '<i class="fa-solid fa-trash-can"></i> Delete';
                    }
                } catch (delErr) {
                    alert(`Network error: ${delErr.message}`);
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fa-solid fa-trash-can"></i> Delete';
                }
            });
        });
    }

    // Filter documents table in real time
    if (filterDocsInput) {
        filterDocsInput.addEventListener('input', (e) => {
            const term = e.target.value.toLowerCase().trim();
            if (!term) {
                renderDocsTable(indexedDocsCache);
                return;
            }
            const filtered = indexedDocsCache.filter(doc => 
                doc.name.toLowerCase().includes(term) || 
                doc.category.toLowerCase().includes(term)
            );
            renderDocsTable(filtered);
        });
    }

    if (btnRefreshLibrary) {
        btnRefreshLibrary.addEventListener('click', () => {
            fetchIndexedDocuments();
            fetchQueueStatus();
        });
    }

    // Initial background status check
    fetchQueueStatus();
}

function escapeHtml(str) {
    if (!str) return '';
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}


// -------------------------------------------------------------
// Visual Image Results & Modal Handlers (Rank-wise & Downloadable)
// -------------------------------------------------------------
function renderImageResults(images) {
    if (!images || images.length === 0) return;

    if (currentStatusLog) {
        currentStatusLog.remove();
        currentStatusLog = null;
    }
    if (currentAssistantBubble) {
        const typingInd = currentAssistantBubble.querySelector('.typing-indicator');
        if (typingInd) typingInd.remove();
    }

    const container = document.createElement('div');
    container.className = 'image-results-container';

    const header = document.createElement('div');
    header.className = 'image-results-header';
    header.innerHTML = `<i class="fa-solid fa-images"></i> Ranked Visual Results (${images.length} Image${images.length > 1 ? 's' : ''})`;
    container.appendChild(header);

    const grid = document.createElement('div');
    grid.className = 'image-results-grid';

    images.forEach((img, idx) => {
        const rankNum = img.rank || (idx + 1);
        const card = document.createElement('div');
        card.className = 'image-card';

        const thumbWrapper = document.createElement('div');
        thumbWrapper.className = 'image-thumb-wrapper';
        thumbWrapper.title = `Click to zoom or download ${img.filename}`;
        thumbWrapper.innerHTML = `
            <img src="${img.image_url}" alt="${img.filename}" loading="lazy">
            <span class="image-rank-badge"><i class="fa-solid fa-trophy"></i> Rank #${rankNum}</span>
            <div class="image-zoom-overlay"><i class="fa-solid fa-magnifying-glass-plus"></i> Click to Zoom</div>
        `;
        thumbWrapper.addEventListener('click', () => {
            openImageModal(img.image_url, img.filename, img.summary || img.point1);
        });

        const body = document.createElement('div');
        body.className = 'image-card-body';

        body.innerHTML = `
            <div class="image-card-header">
                <span class="image-card-filename" title="${img.filename}">${escapeHtml(img.filename)}</span>
                <span class="image-cat-pill">${escapeHtml(img.category || 'General')}</span>
            </div>
            <div class="image-card-points">
                <div><span class="image-point-tag visual">Visual</span> ${escapeHtml(img.point1 || 'Visual content')}</div>
                <div><span class="image-point-tag text">Text</span> ${escapeHtml(img.point2 || 'None detected')}</div>
                <div><span class="image-point-tag context">Context</span> ${escapeHtml(img.point3 || 'General')}</div>
            </div>
            <div class="image-card-actions">
                <a href="${img.image_url}" download="${img.filename}" class="btn-image-download" title="Download ${img.filename}">
                    <i class="fa-solid fa-download"></i> Download Image
                </a>
            </div>
        `;

        card.appendChild(thumbWrapper);
        card.appendChild(body);
        grid.appendChild(card);
    });

    container.appendChild(grid);

    // Append to mediaContainer so streamed text tokens never overwrite images
    if (currentMediaContainer) {
        currentMediaContainer.innerHTML = '';
        currentMediaContainer.appendChild(container);
    } else if (currentAssistantBubble) {
        currentAssistantBubble.appendChild(container);
    } else {
        chatMessages.appendChild(container);
    }
    scrollToBottom();
}

function openImageModal(src, filename, caption) {
    const modal = document.getElementById('image-modal');
    const modalImg = document.getElementById('image-modal-img');
    const modalCaption = document.getElementById('image-modal-caption');
    if (modal && modalImg) {
        modalImg.src = src;
        modalCaption.innerHTML = `
            <div class="modal-caption-text"><strong>${escapeHtml(filename)}</strong>: ${escapeHtml(caption || '')}</div>
            <div class="modal-actions" style="margin-top: 10px;">
                <a href="${src}" download="${filename}" class="btn-image-download modal-download-btn" style="max-width: 250px; margin: 0 auto; display: inline-flex;">
                    <i class="fa-solid fa-download"></i> Download Original Image
                </a>
            </div>
        `;
        modal.style.display = 'flex';
    }
}
