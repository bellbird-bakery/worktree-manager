/**
 * Kanban Board - Drag and Drop for Worktree Task Tracker
 *
 * Adapted from the main project's reusable Kanban module.
 */

class KanbanBoard {
    /**
     * Create a new KanbanBoard instance.
     * @param {HTMLElement} container - The .kanban-board container element
     */
    constructor(container) {
        this.container = container;
        this.moveUrlTemplate = container.dataset.moveUrl;
        this.csrfToken = container.dataset.csrfToken ||
            document.querySelector('meta[name="csrf-token"]')?.content;

        if (!this.moveUrlTemplate) {
            console.warn('KanbanBoard: No data-move-url specified');
        }

        this.init();
    }

    /**
     * Initialize event listeners for all cards and columns.
     */
    init() {
        // Set up drag handlers for all cards
        this.container.querySelectorAll('.kanban-card').forEach(card => {
            this.setupDraggable(card);
        });

        // Set up drop zones for all columns
        this.container.querySelectorAll('.kanban-column-content').forEach(column => {
            this.setupDropZone(column);
        });
    }

    /**
     * Make a card draggable.
     * @param {HTMLElement} card - The card element to make draggable
     */
    setupDraggable(card) {
        card.setAttribute('draggable', 'true');

        card.addEventListener('dragstart', (e) => {
            card.classList.add('dragging');
            e.dataTransfer.setData('text/plain', card.dataset.itemId);
            e.dataTransfer.effectAllowed = 'move';
        });

        card.addEventListener('dragend', () => {
            card.classList.remove('dragging');
            // Clean up any lingering drag-over states
            this.container.querySelectorAll('.drag-over').forEach(el => {
                el.classList.remove('drag-over');
            });
        });
    }

    /**
     * Set up a column as a drop zone.
     * @param {HTMLElement} column - The column content element
     */
    setupDropZone(column) {
        column.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            column.classList.add('drag-over');
        });

        column.addEventListener('dragleave', (e) => {
            // Only remove drag-over if leaving the column entirely
            if (!column.contains(e.relatedTarget)) {
                column.classList.remove('drag-over');
            }
        });

        column.addEventListener('drop', async (e) => {
            e.preventDefault();
            column.classList.remove('drag-over');

            const itemId = e.dataTransfer.getData('text/plain');
            const newStatus = column.dataset.status;

            await this.moveItem(itemId, newStatus);
        });
    }

    /**
     * Move an item to a new status via API call.
     * @param {string} itemId - The ID of the item to move
     * @param {string} newStatus - The target status value
     */
    async moveItem(itemId, newStatus) {
        const card = this.container.querySelector(`[data-item-id="${itemId}"]`);
        if (!card) {
            console.error('KanbanBoard: Card not found for item', itemId);
            return;
        }

        const oldStatus = card.dataset.status;

        // Skip if dropping in same column
        if (oldStatus === newStatus) {
            return;
        }

        // Show loading state
        card.classList.add('updating');

        // Build URL from template
        const url = this.moveUrlTemplate.replace('{id}', itemId);

        try {
            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-CSRFToken': this.csrfToken,
                },
                body: `status=${encodeURIComponent(newStatus)}`,
            });

            const data = await response.json().catch(() => null);

            // Check if hook needs confirmation
            if (data && data.needs_confirmation) {
                card.classList.remove('updating');
                this.showConfirmationModal(itemId, oldStatus, newStatus, data);
                return;
            }

            if (response.ok && data && data.success) {
                // Move card to new column in DOM
                this.moveCardInDOM(card, oldStatus, newStatus);

                // Show hook results as toast if any
                if (data.hooks && data.hooks.length > 0) {
                    this.showHookResults(data.hooks);
                }
            } else {
                // Handle error
                const errorText = data?.error || data?.message || 'Move failed';
                this.showError(errorText);
            }
        } catch (error) {
            console.error('KanbanBoard: Move failed', error);
            this.showError('Network error - please try again');
        } finally {
            card.classList.remove('updating');
        }
    }

    /**
     * Move a card element to a new column in the DOM.
     * @param {HTMLElement} card - The card element
     * @param {string} oldStatus - The previous status
     * @param {string} newStatus - The new status
     */
    moveCardInDOM(card, oldStatus, newStatus) {
        const targetColumn = this.container.querySelector(
            `.kanban-column-content[data-status="${newStatus}"]`
        );

        if (targetColumn) {
            // Remove "No tasks" placeholder if present
            const emptyMsg = targetColumn.querySelector('.kanban-empty');
            if (emptyMsg) {
                emptyMsg.remove();
            }

            targetColumn.appendChild(card);
            card.dataset.status = newStatus;

            // Add empty message to old column if needed
            this.checkEmptyColumn(oldStatus);

            // Update column counts
            this.updateCounts();
        }
    }

    /**
     * Show confirmation modal for hooks that need user input.
     * @param {string} itemId - The task ID
     * @param {string} oldStatus - The previous status
     * @param {string} newStatus - The target status
     * @param {Object} data - The confirmation data from server
     */
    showConfirmationModal(itemId, oldStatus, newStatus, data) {
        // Remove existing modal if any
        const existingModal = document.getElementById('hookConfirmModal');
        if (existingModal) {
            existingModal.remove();
        }

        const confirmData = data.data || {};
        let modalContent = '';

        if (data.confirmation_type === 'git_commit') {
            modalContent = `
                <div class="modal-header">
                    <h5 class="modal-title">
                        <i class="bi bi-git me-2"></i>Commit Changes?
                    </h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <p>You have uncommitted changes in this worktree:</p>
                    ${confirmData.status_short ? `<pre class="bg-light p-2 small">${this.escapeHtml(confirmData.status_short)}</pre>` : ''}
                    ${confirmData.diff_stat ? `<pre class="bg-light p-2 small">${this.escapeHtml(confirmData.diff_stat)}</pre>` : ''}
                    <div class="mt-3">
                        <label for="commitMessage" class="form-label">Commit message:</label>
                        <input type="text" class="form-control" id="commitMessage"
                               value="${this.escapeHtml(confirmData.default_message || '')}"
                               placeholder="Enter commit message">
                    </div>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-action="skip">
                        Skip Commit
                    </button>
                    <button type="button" class="btn btn-primary" data-action="confirm">
                        <i class="bi bi-check-lg me-1"></i>Commit & Complete
                    </button>
                </div>
            `;
        } else {
            // Generic confirmation
            modalContent = `
                <div class="modal-header">
                    <h5 class="modal-title">Confirm Action</h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <p>This action requires confirmation.</p>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-action="skip">Skip</button>
                    <button type="button" class="btn btn-primary" data-action="confirm">Confirm</button>
                </div>
            `;
        }

        // Create modal HTML
        const modalHtml = `
            <div class="modal fade" id="hookConfirmModal" tabindex="-1">
                <div class="modal-dialog">
                    <div class="modal-content">
                        ${modalContent}
                    </div>
                </div>
            </div>
        `;

        document.body.insertAdjacentHTML('beforeend', modalHtml);
        const modal = new bootstrap.Modal(document.getElementById('hookConfirmModal'));

        // Handle button clicks
        const modalEl = document.getElementById('hookConfirmModal');
        modalEl.querySelectorAll('[data-action]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const action = btn.dataset.action;
                const commitMessage = modalEl.querySelector('#commitMessage')?.value || '';

                modal.hide();
                await this.confirmHook(itemId, oldStatus, newStatus, action, commitMessage);
            });
        });

        // Clean up modal on hidden
        modalEl.addEventListener('hidden.bs.modal', () => {
            modalEl.remove();
        });

        modal.show();
    }

    /**
     * Send hook confirmation to server.
     * @param {string} itemId - The task ID
     * @param {string} oldStatus - The previous status
     * @param {string} newStatus - The target status
     * @param {string} action - 'confirm' or 'skip'
     * @param {string} commitMessage - The commit message (for git hook)
     */
    async confirmHook(itemId, oldStatus, newStatus, action, commitMessage) {
        const card = this.container.querySelector(`[data-item-id="${itemId}"]`);
        if (card) {
            card.classList.add('updating');
        }

        const url = `/tasks/${itemId}/confirm/`;

        try {
            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-CSRFToken': this.csrfToken,
                },
                body: new URLSearchParams({
                    action: action,
                    old_status: oldStatus,
                    new_status: newStatus,
                    commit_message: commitMessage,
                }),
            });

            const data = await response.json().catch(() => null);

            if (response.ok && data && data.success) {
                if (card) {
                    this.moveCardInDOM(card, oldStatus, newStatus);
                }

                if (data.hooks && data.hooks.length > 0) {
                    this.showHookResults(data.hooks);
                }
            } else {
                this.showError(data?.error || 'Confirmation failed');
            }
        } catch (error) {
            console.error('KanbanBoard: Confirmation failed', error);
            this.showError('Network error - please try again');
        } finally {
            if (card) {
                card.classList.remove('updating');
            }
        }
    }

    /**
     * Show hook execution results as toast notifications.
     * @param {Array} hooks - Array of hook results
     */
    showHookResults(hooks) {
        hooks.forEach(hook => {
            if (hook.name && hook.message) {
                // Simple console log for now - could be enhanced with toasts
                console.log(`Hook ${hook.name}: ${hook.success ? 'OK' : 'Failed'} - ${hook.message}`);
            }
        });
    }

    /**
     * Escape HTML to prevent XSS.
     * @param {string} text - Text to escape
     * @returns {string} Escaped text
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    /**
     * Check if a column is empty and add placeholder if needed.
     * @param {string} status - The status of the column to check
     */
    checkEmptyColumn(status) {
        const column = this.container.querySelector(
            `.kanban-column-content[data-status="${status}"]`
        );

        if (column && !column.querySelector('.kanban-card')) {
            // Column is now empty, add placeholder
            const emptyDiv = document.createElement('div');
            emptyDiv.className = 'kanban-empty';
            emptyDiv.innerHTML = '<i class="bi bi-inbox text-muted"></i><span>No tasks</span>';
            column.appendChild(emptyDiv);
        }
    }

    /**
     * Update the count badges in all column headers.
     */
    updateCounts() {
        this.container.querySelectorAll('.kanban-column').forEach(column => {
            const status = column.dataset.status;
            const count = column.querySelectorAll('.kanban-card').length;
            const badge = column.querySelector('.kanban-count');

            if (badge) {
                badge.textContent = count;
            }
        });
    }

    /**
     * Show an error message to the user.
     * @param {string} message - The error message to display
     */
    showError(message) {
        // Simple alert for now - could be enhanced with toast notifications
        alert('Error: ' + message);
    }
}

// Auto-initialize on DOMContentLoaded
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.kanban-board').forEach(board => {
        // Store instance on element for potential external access
        board._kanbanBoard = new KanbanBoard(board);
    });
});
