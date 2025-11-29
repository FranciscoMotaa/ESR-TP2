import random
import datetime
import pickle


class Message:
    START_VIDEO        = 1   # data -> {"video": nome do video, "port": porta a enviar a stream}
    CHECK_ALL_VIDEOS   = 2   # vazio
    STOP_VIDEO         = 3   # data -> {"video": nome do video}
    A_CHECK_ALL_VIDEOS = 4   # data -> {"videos": [lista dos videos]}
    REGISTER           = 5   # data -> {"id": nodeid}
    A_REGISTER         = 6   # data -> {"neighbours": {neighbours ips}}
    ACK                = 7   # vazio
    ERROR              = 8   # data -> {"reason": "descricao"}
    FLOOD              = 9   # data -> {"timestamp:" timestamp}
    HEARTBEAT          = 10  # data -> {"ip": ip}


    def __init__(self, type, data="", source=""):
        self.id = random.randint(0, 1000)
        self.type = type
        self.data = data
        self.timestamp = datetime.datetime.now()


    def get_type(self):
        return self.type


    def serialize(self):
        return pickle.dumps(self)
        
    @staticmethod
    def deserialize(data):
        return pickle.loads(data)
        

    def __str__(self):
        return f"{{\n\tType: {self.type},\n\tId: {self.id},\n\tSource: {self.source},\n\tData: {self.data}, \n\tTimestamp: {self.timestamp}\n}}"