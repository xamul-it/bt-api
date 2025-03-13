from flask import Flask, send_from_directory, abort, request, Blueprint
import os
import logging

fs_bp = Blueprint('fileserver', __name__)
logger = logging.getLogger(__name__)

# Definizione dei percorsi delle cartelle
DATA_DIRECTORY = "./config"
BACKUP_DIRECTORY = "./backup"


@fs_bp.route('/data/<path:filename>')
def serve_data(filename):
    """
    Serve file dalla cartella ./config/.
    """
    print(f'Request {filename}')
    return serve_file(DATA_DIRECTORY, filename)

@fs_bp.route('/backup/<path:filename>')
def serve_backup(filename):
    """
    Serve file dalla cartella ./backup/.
    """
    print(f'Request {filename}')
    return serve_file(BACKUP_DIRECTORY, filename)

def serve_file(directory, filename):
    """
    Funzione di utilità per servire file in modo sicuro.
    Verifica che il file esista e lo serve usando send_from_directory.
    """
    # Verifica che il percorso del file sia sicuro
    if not is_safe_path(directory, filename):
        abort(403)  # Accesso negato per percorsi non sicuri

    # Verifica che il file esista
    file_path = os.path.join(directory, filename)
    if not os.path.exists(file_path):
        abort(404)  # File non trovato

    # Servi il file
    return send_from_directory(directory, filename)

def is_safe_path(base_directory, requested_path):
    """
    Verifica che il percorso richiesto sia sicuro e non tenti di accedere
    a directory esterne a base_directory.
    """
    # Risolve il percorso assoluto
    requested_abs_path = os.path.abspath(os.path.join(base_directory, requested_path))
    base_abs_path = os.path.abspath(base_directory)

    # Verifica che il percorso richiesto sia all'interno della directory base
    return requested_abs_path.startswith(base_abs_path)
