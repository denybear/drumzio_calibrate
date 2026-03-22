# drumzio_calibrate
This project is to be used to calibrate the drum in drumzio project

# The parameters used to track drum hit (drum_trigger_cfg_t structure)

* th_high_head  
  - Seuil haut pour la tête (head). Une valeur ADC au-dessus de ce seuil déclenche le démarrage possible d’un groupe de détection pour la voie head (détection du début de frappe).

* th_low_head  
  - Seuil bas pour la tête. Quand la valeur ADC descend en dessous de ce seuil, la voie head est considérée “au repos” (contribue au calcul de release).

* th_high_rim  
  - Seuil haut pour le rim. Même rôle que th_high_head mais pour la voie rim (déclenchement du début du groupe depuis le rim).

* th_low_rim  
  - Seuil bas pour le rim. Même rôle que th_low_head mais pour la voie rim (détection de la fin locale).

* scan_min_ms  
  - Durée minimale (en ms) à observer après le démarrage du groupe avant d’autoriser la décision de fin de groupe. Empêche de clore immédiatement une frappe trop rapidement (assure un intervalle minimal d’échantillonnage).

* release_ms  
  - Temps (en ms) pendant lequel toutes les voies doivent rester en dessous de leur seuil bas pour considérer que la frappe est “relâchée” et permettre la fin du groupe. C’est le délai de dégagement (debounce/fin).

* max_group_ms  
  - Durée maximale (en ms) d’un groupe de détection : sécurité qui force la terminaison du groupe si trop long (évite blocage si signal reste élevé indéfiniment).

* retrigger_head_ms  
  - Temps de masque / anti-retrigger pour la voie head : après qu’une frappe impliquant la head ait été rapportée, la voie head est ignorée pour cette durée pour éviter les doubles déclenchements successifs.

* retrigger_rim_ms  
  - Temps de masque / anti-retrigger pour la voie rim : même comportement que retrigger_head_ms mais pour la voie rim.

* both_ratio_q15  
  - Seuil de ratio (format Q15) utilisé pour décider si une frappe où les deux voies dépassent leur seuil haut doit être classée BOTH (rimshot/simultané) ou dominante (head ou rim). On calcule max/min en Q15 ; si ratio <= both_ratio_q15 (et la secondaire est suffisamment grande), on déclare BOTH. Valeurs >1 (ex. 1.5 -> 1.5*32768) signifient tolérance d’asymétrie.

* min_secondary_for_both  
  - Seuil minimum (valeur ADC) exigé sur la voie secondaire pour qu’une frappe potentiellement BOTH soit acceptée comme BOTH. Empêche de classer BOTH quand la seconde voie est trop faible (bruit ou réverbération).
